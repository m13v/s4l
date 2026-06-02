#!/usr/bin/env python3
"""
Fetch GSC search queries for a product and upsert into the gsc_queries Postgres table.

State schema (gsc_queries table):
  product, query, impressions, clicks, ctr, position,
  status (pending | in_progress | done | skip | duplicate),
  page_slug, page_url, notes,
  first_seen, last_seen, completed_at, created_at, updated_at

Usage:
  python3 fetch_gsc_queries.py --product Fazm
  python3 fetch_gsc_queries.py --product Fazm --dry-run
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(ROOT_DIR, "config.json")
SA_PATH = os.path.join(SCRIPT_DIR, "credentials", "seo-autopilot-sa.json")

sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
from http_api import api_get, api_post, load_env  # noqa: E402

PERIOD_DAYS = 90
ROW_LIMIT = 25000
IMPRESSIONS_THRESHOLD = 5


def get_product_config(product_name):
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    for p in config.get("projects", []):
        if p["name"].lower() == product_name.lower():
            return p
    return None


def get_gsc_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        SA_PATH,
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    return build("searchconsole", "v1", credentials=creds)


def fetch_gsc_rows(gsc_property):
    svc = get_gsc_service()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=PERIOD_DAYS)).strftime("%Y-%m-%d")
    result = svc.searchanalytics().query(
        siteUrl=gsc_property,
        body={
            "startDate": start,
            "endDate": end,
            "dimensions": ["query"],
            "rowLimit": ROW_LIMIT,
        },
    ).execute()
    return result.get("rows", [])


def classify(query_text, brand_terms):
    q = query_text.lower().strip()
    if q in {t.lower() for t in brand_terms}:
        return "skip"
    return "pending"


def upsert(product, rows, brand_terms, dry_run=False):
    if dry_run:
        added = sum(1 for r in rows if int(r["impressions"]) >= IMPRESSIONS_THRESHOLD)
        print(f"[fetch_gsc_queries] DRY RUN: would upsert {len(rows)} queries ({added} above threshold)")
        return len(rows), 0

    payload_rows = [
        {
            "query": row["keys"][0],
            "impressions": int(row["impressions"]),
            "clicks": int(row["clicks"]),
            "ctr": float(row.get("ctr", 0)),
            "position": float(row.get("position", 0)),
            "status": classify(row["keys"][0], brand_terms),
        }
        for row in rows
    ]
    if not payload_rows:
        return 0, 0

    resp = api_post("/api/v1/seo/gsc-queries", {"product": product, "rows": payload_rows})
    data = resp.get("data") or {}
    return int(data.get("added") or 0), int(data.get("updated") or 0)


def print_summary(product, dry_run=False):
    if dry_run:
        return
    resp = api_get("/api/v1/seo/gsc-queries", query={"mode": "stats", "product": product})
    by_status = (resp.get("data") or {}).get("by_status") or []
    counts = {r.get("status"): int(r.get("count") or 0) for r in by_status}
    pending = counts.get("pending", 0)
    done = counts.get("done", 0)
    skip = counts.get("skip", 0)
    in_progress = counts.get("in_progress", 0)
    print(f"[fetch_gsc_queries] pending={pending} done={done} skip={skip} in_progress={in_progress}")

    resp = api_get("/api/v1/seo/gsc-queries",
                   query={"mode": "top_pending", "product": product, "limit": 10})
    rows = (resp.get("data") or {}).get("rows") or []
    if rows:
        print(f"\n[fetch_gsc_queries] Top 10 pending queries (>={IMPRESSIONS_THRESHOLD} impr):")
        for r in rows:
            print(f"  {int(r.get('impressions') or 0):>5} impr  {int(r.get('clicks') or 0):>4} clk  {r.get('query')}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", required=True, help="Product name (e.g. Fazm)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    product_cfg = get_product_config(args.product)
    if not product_cfg:
        print(f"[fetch_gsc_queries] ERROR: product '{args.product}' not found in config.json")
        sys.exit(1)

    # Normalize to canonical name from config so DB writes never diverge by casing
    product = product_cfg["name"]

    gsc_property = product_cfg.get("landing_pages", {}).get("gsc_property")
    if not gsc_property:
        print(f"[fetch_gsc_queries] ERROR: no gsc_property configured for {product}")
        sys.exit(1)

    brand_terms = product_cfg.get("landing_pages", {}).get("brand_terms", [])

    print(f"[fetch_gsc_queries] Fetching last {PERIOD_DAYS} days of queries for {product} ({gsc_property})")
    rows = fetch_gsc_rows(gsc_property)
    print(f"[fetch_gsc_queries] API returned {len(rows)} queries")

    added, updated = upsert(product, rows, brand_terms, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"[fetch_gsc_queries] added={added} updated={updated} total={len(rows)}")

    print_summary(product, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
