#!/usr/bin/env python3
"""
Database helpers for the SEO pipeline.

All state reads/writes go through the s4l.ai HTTP API (/api/v1/seo/keywords),
not Postgres directly (HTTP-only, 2026-06-01). No DATABASE_URL on the operator
box. forbidden-keyword matching stays local (reads config.json).
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))

from http_api import api_get, api_patch, load_env  # noqa: E402

load_env()


def load_forbidden_keywords(product):
    """Return the list of forbidden-keyword patterns configured for this product.

    Reads from config.json projects[].landing_pages.forbidden_keywords. Matching
    is case-insensitive substring; the patterns are meant to be surface-form
    search fragments (e.g. 'body scan'), not regex.
    """
    cfg_path = os.path.join(ROOT_DIR, "config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    for p in cfg.get("projects", []):
        if p.get("name", "").lower() == (product or "").lower():
            lp = p.get("landing_pages") or {}
            return [str(x).lower() for x in (lp.get("forbidden_keywords") or [])]
    return []


def match_forbidden(product, keyword):
    """Return the first forbidden pattern matching this keyword, or ''.

    Case-insensitive substring match. '' means not forbidden.
    """
    kw = (keyword or "").lower()
    for pattern in load_forbidden_keywords(product):
        if pattern and pattern in kw:
            return pattern
    return ""


def pick_next_keyword(product):
    """Pick next keyword: pending (ready to build) first, then unscored."""
    resp = api_get("/api/v1/seo/keywords", query={"mode": "pick", "product": product})
    return resp.get("data")


def update_status(product, keyword, status, **kwargs):
    """Update keyword status and optional fields."""
    body = {"product": product, "keyword": keyword, "status": status}
    for field in ("score", "signal1", "signal2", "signal3", "notes",
                  "page_url", "slug", "content_type", "claude_session_id"):
        if field in kwargs:
            body[field] = kwargs[field]
    api_patch("/api/v1/seo/keywords", body)


def check_slug_exists(product, slug):
    """Check if a page with this slug already exists (status=done)."""
    resp = api_get("/api/v1/seo/keywords",
                   query={"mode": "check_slug", "product": product, "slug": slug})
    return bool((resp.get("data") or {}).get("exists"))


def list_done_pages(product, limit=400):
    """Return the inventory of completed pages for this product.

    Used by generate_page.py's build_prompt to give the model the choice
    between writing a new page or consolidating into an existing one.
    Returns a list of dicts ordered by completion recency (newest first).
    """
    resp = api_get("/api/v1/seo/keywords",
                   query={"mode": "list_done", "product": product, "limit": int(limit)})
    rows = (resp.get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        out.append({
            "slug": r.get("slug"),
            "keyword": r.get("keyword"),
            "page_url": r.get("page_url"),
            "content_type": r.get("content_type"),
            "completed_at": r.get("completed_at"),
        })
    return out


def has_work(product):
    """Check if there's any work to do for a product."""
    resp = api_get("/api/v1/seo/keywords", query={"mode": "has_work", "product": product})
    return bool((resp.get("data") or {}).get("has_work"))


def report(product):
    """Print status summary for a product."""
    resp = api_get("/api/v1/seo/keywords", query={"mode": "report", "product": product})
    data = resp.get("data") or {}
    by_status = data.get("by_status") or []
    total = sum(int(r.get("count") or 0) for r in by_status)
    print(f"  Total keywords: {total}")
    for r in by_status:
        print(f"  {r.get('status')}: {r.get('count')}")
    pending = data.get("top_pending") or []
    if pending:
        print("  Top pending:")
        for r in pending:
            score = r.get("score")
            print(f"    {float(score):.1f} | {r.get('keyword')}")


if __name__ == "__main__":
    # CLI: python3 db_helpers.py <command> <product> [args]
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    product = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "pick":
        result = pick_next_keyword(product)
        print(json.dumps(result) if result else "NONE")
    elif cmd == "update":
        keyword = sys.argv[3]
        status = sys.argv[4]
        update_status(product, keyword, status)
    elif cmd == "has_work":
        print("yes" if has_work(product) else "no")
    elif cmd == "report":
        report(product)
    elif cmd == "check_slug":
        slug = sys.argv[3]
        print("exists" if check_slug_exists(product, slug) else "new")
    elif cmd == "check_forbidden":
        keyword = sys.argv[3]
        match = match_forbidden(product, keyword)
        print(match if match else "ok")
    else:
        print("Usage: db_helpers.py <pick|update|has_work|report|check_slug|check_forbidden> <product> [args]")
