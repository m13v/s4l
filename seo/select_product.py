#!/usr/bin/env python3
"""Weighted-random product picker for SEO pipelines.

Single source of truth for eligibility and weighted selection,
used by both cron_seo.sh (SERP) and cron_gsc.sh (GSC).

Modes decide which products count as "eligible":
  any           — repo exists on disk (default; original behavior)
  unseeded      — repo exists AND has ZERO rows in seo_keywords
                  (used to bootstrap new products into the SERP pipeline)
  serp-generate — repo exists AND has seo_keywords with status=pending, score>=1.5
  serp-score    — repo exists AND has seo_keywords with status=unscored
  gsc           — repo exists AND has landing_pages.gsc_property AND
                  has gsc_queries with status=pending, impressions>=5
  unseeded-gsc  — repo exists AND has gsc_property AND has ZERO rows in gsc_queries
                  (used to bootstrap new products into the GSC pipeline)

If multiple products qualify, pick by config weight (same as before).
If none qualify, prints "NONE" so the caller can try a fallback mode.

Usage:
    select_product.py                        # any eligible (repo exists)
    select_product.py --mode serp-generate   # only products with pending SERP work
    select_product.py --mode serp-score      # only products with unscored SERP work
    select_product.py --mode gsc             # only products with pending GSC work
    select_product.py --require-gsc          # legacy flag, == --mode gsc-configured
                                             # (repo + gsc_property, not work check)
"""

import argparse
import json
import os
import random
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
from http_api import api_get  # noqa: E402

GSC_IMPRESSIONS_THRESHOLD = 5
SERP_SCORE_THRESHOLD = 1.5  # raised 2026-05-05 from 1.0 to cut dead-weight pages dragging domain quality


def base_eligible(config, require_gsc_property=False):
    """Products with repo on disk (and optionally gsc_property configured)."""
    out = []
    for p in config.get("projects", []):
        lp = p.get("landing_pages", {})
        repo = lp.get("repo", "")
        if not repo or not os.path.isdir(os.path.expanduser(repo)):
            continue
        if require_gsc_property and not lp.get("gsc_property"):
            continue
        out.append((p["name"], p.get("weight", 1)))
    return out


def products_with_serp_work(candidates, status, score_filter=False):
    """Filter candidates to those with at least one seo_keywords row matching."""
    names = [n for n, _ in candidates]
    if not names:
        return []
    query = {"mode": "with_serp_work", "products": ",".join(names), "status": status}
    if score_filter:
        query["score_filter"] = "1"
    resp = api_get("/api/v1/seo/keywords", query=query)
    with_work = set((resp.get("data") or {}).get("products") or [])
    return [(n, w) for n, w in candidates if n in with_work]


def products_without_keywords(candidates):
    """Filter candidates to those with ZERO rows in seo_keywords."""
    names = [n for n, _ in candidates]
    if not names:
        return []
    resp = api_get("/api/v1/seo/keywords",
                   query={"mode": "without_keywords", "products": ",".join(names)})
    without = set((resp.get("data") or {}).get("products") or [])
    return [(n, w) for n, w in candidates if n in without]


def products_without_gsc_queries(candidates):
    """Filter candidates to those with ZERO rows in gsc_queries."""
    names = [n for n, _ in candidates]
    if not names:
        return []
    resp = api_get("/api/v1/seo/gsc-queries",
                   query={"mode": "without", "products": ",".join(names)})
    without = set((resp.get("data") or {}).get("products") or [])
    return [(n, w) for n, w in candidates if n in without]


def products_with_gsc_work(candidates):
    """Filter candidates to those with pending gsc_queries rows >= threshold impressions."""
    names = [n for n, _ in candidates]
    if not names:
        return []
    resp = api_get("/api/v1/seo/gsc-queries",
                   query={"mode": "with_work", "products": ",".join(names)})
    with_work = set((resp.get("data") or {}).get("products") or [])
    return [(n, w) for n, w in candidates if n in with_work]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["any", "unseeded", "serp-generate", "serp-score",
                                       "gsc", "unseeded-gsc"],
                    default="any",
                    help="Eligibility mode (default: any)")
    ap.add_argument("--require-gsc", action="store_true",
                    help="Legacy: only pick products with gsc_property set (no work check)")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    # Legacy flag: keep working for callers that just want gsc-configured products
    if args.require_gsc and args.mode == "any":
        picks = base_eligible(config, require_gsc_property=True)
    elif args.mode == "gsc":
        candidates = base_eligible(config, require_gsc_property=True)
        picks = products_with_gsc_work(candidates)
    elif args.mode == "unseeded":
        candidates = base_eligible(config)
        picks = products_without_keywords(candidates)
    elif args.mode == "unseeded-gsc":
        candidates = base_eligible(config, require_gsc_property=True)
        picks = products_without_gsc_queries(candidates)
    elif args.mode == "serp-generate":
        candidates = base_eligible(config)
        picks = products_with_serp_work(candidates, status="pending", score_filter=True)
    elif args.mode == "serp-score":
        candidates = base_eligible(config)
        picks = products_with_serp_work(candidates, status="unscored")
    else:
        picks = base_eligible(config)

    if not picks:
        print("NONE")
        return

    names, weights = zip(*picks)
    print(random.choices(names, weights=weights, k=1)[0])


if __name__ == "__main__":
    main()
