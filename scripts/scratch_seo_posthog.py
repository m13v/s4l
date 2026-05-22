#!/usr/bin/env python3
"""Query PostHog for pageviews on Twitter-generated SEO pages.

For each project, group SEO page URLs by (posthog.project_id, api_key_env),
then issue a single HogQL query per bucket that filters by URL set.
"""
import os, sys, json, subprocess, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

dbmod.load_env()
import psycopg2
import psycopg2.extras
import urllib.request
import urllib.error

CONFIG = os.path.expanduser("~/social-autoposter/config.json")

def keychain(name):
    try:
        v = subprocess.check_output(
            ["security", "find-generic-password", "-s", name, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return v
    except subprocess.CalledProcessError:
        return None

with open(CONFIG) as f:
    cfg = json.load(f)
projects = {p["name"]: p for p in cfg.get("projects", [])}

# Fetch the SEO pages list
conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
SELECT product, page_url
FROM seo_keywords
WHERE source='twitter' AND page_url IS NOT NULL AND page_url <> ''
""")
pages_by_product = {}
for r in cur.fetchall():
    pages_by_product.setdefault(r["product"], []).append(r["page_url"])
cur.close()
conn.close()

# Group projects sharing the same posthog (project_id, api_key_env)
# so we issue one query per bucket.
buckets = {}  # (project_id, api_key_env) -> {"projects":[name], "urls":[]}
for prod, urls in pages_by_product.items():
    proj = projects.get(prod) or {}
    ph = proj.get("posthog") or {}
    pid = str(ph.get("project_id") or "")
    env_name = (ph.get("api_key_env") or "POSTHOG_PERSONAL_API_KEY").strip()
    if not pid:
        print(f"[skip] {prod}: no posthog project_id", file=sys.stderr)
        continue
    key_env = env_name or "POSTHOG_PERSONAL_API_KEY"
    api_key = os.environ.get(key_env) or keychain(key_env)
    if not api_key:
        # Fallback for keychain non-env name
        if key_env == "FAZM_POSTHOG_API_KEY":
            api_key = keychain("FAZM_POSTHOG_API_KEY")
        if not api_key:
            print(f"[skip] {prod}: api key {key_env} not found", file=sys.stderr)
            continue
    bk = (pid, key_env)
    if bk not in buckets:
        buckets[bk] = {"api_key": api_key, "products": set(), "urls": []}
    buckets[bk]["products"].add(prod)
    buckets[bk]["urls"].extend(urls)

def hogql(api_key, pid, query, timeout=60):
    url = f"https://us.posthog.com/api/projects/{pid}/query/"
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("results", []) or []
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = ""
        print(f"  HTTPError {e.code}: {detail}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  Error: {e}", file=sys.stderr)
        return None

def url_to_pathnorm(u):
    """PostHog $pathname is path only (no host). Need to filter on $current_url."""
    return u.rstrip("/")

def query_bucket(api_key, pid, urls, days):
    """Return pageviews per URL for last N days."""
    if not urls:
        return {}
    # Normalize: strip trailing slash
    norm = list({u.rstrip("/") for u in urls})
    # PostHog stores $current_url with or without trailing slash. Use IN with both forms.
    safe = []
    for u in norm:
        e1 = u.replace("'", "''")
        e2 = (u + "/").replace("'", "''")
        safe.append(f"'{e1}'"); safe.append(f"'{e2}'")
    in_list = ", ".join(safe)
    q = f"""
SELECT
  replaceRegexpOne(properties.$current_url, '/$', '') AS url,
  count() AS pageviews,
  count(DISTINCT distinct_id) AS unique_users
FROM events
WHERE event = '$pageview'
  AND timestamp >= now() - INTERVAL {int(days)} DAY
  AND properties.$current_url IN ({in_list})
GROUP BY url
ORDER BY pageviews DESC
"""
    res = hogql(api_key, pid, q)
    if res is None:
        return None
    out = {}
    for row in res:
        url, pv, uu = row[0], int(row[1]), int(row[2])
        out[url] = {"pageviews": pv, "unique": uu}
    return out

results_7d = {}
results_30d = {}
errors = []
print(f"\n=== buckets ({len(buckets)} groups) ===")
for (pid, env), bk in buckets.items():
    products = sorted(bk["products"])
    n_urls = len(set(bk["urls"]))
    print(f"  pid={pid} key_env={env}: {n_urls} urls across {products}")

print("\n=== querying PostHog 7d ===")
for (pid, env), bk in buckets.items():
    out = query_bucket(bk["api_key"], pid, bk["urls"], days=7)
    if out is None:
        errors.append(f"7d pid={pid} env={env} failed")
        continue
    results_7d.update(out)

print("\n=== querying PostHog 30d ===")
for (pid, env), bk in buckets.items():
    out = query_bucket(bk["api_key"], pid, bk["urls"], days=30)
    if out is None:
        errors.append(f"30d pid={pid} env={env} failed")
        continue
    results_30d.update(out)

print(f"\nerrors: {errors}")

# Aggregate
def total(d):
    return sum(v["pageviews"] for v in d.values()), sum(v["unique"] for v in d.values()), len([v for v in d.values() if v["pageviews"]>0])

pv7, uu7, hits7 = total(results_7d)
pv30, uu30, hits30 = total(results_30d)
print(f"\n=== TOTALS ===")
print(f"  7d : pageviews={pv7}  unique={uu7}  pages_with_traffic={hits7}")
print(f"  30d: pageviews={pv30}  unique={uu30}  pages_with_traffic={hits30}")

# Top 10 pages by 30d pageviews
top = sorted(results_30d.items(), key=lambda kv: kv[1]["pageviews"], reverse=True)[:10]
print(f"\n=== TOP 10 PAGES BY 30D PAGEVIEWS ===")
for url, v in top:
    print(f"  pv={v['pageviews']:5d}  uu={v['unique']:5d}  {url}")

# Save to disk
with open("/tmp/seo_twitter_posthog.json","w") as f:
    json.dump({"7d": results_7d, "30d": results_30d, "errors": errors}, f, indent=2)
print(f"\nwrote /tmp/seo_twitter_posthog.json")
