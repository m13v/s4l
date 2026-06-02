#!/usr/bin/env python3
"""
GSC query-ledger helpers for the SEO pipeline.

All state reads/writes go through the s4l.ai HTTP API (/api/v1/seo/gsc-queries),
not Postgres directly (HTTP-only, 2026-06-01). No DATABASE_URL on the operator
box. Replaces the inline psycopg2 heredocs that used to live in
run_gsc_pipeline.sh (pick next pending query, mark skip/in_progress, summary).
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))

from http_api import api_get, api_patch, load_env  # noqa: E402

load_env()


def pick_next(product):
    """Highest-impression pending query (impressions>=5), or None."""
    resp = api_get("/api/v1/seo/gsc-queries", query={"mode": "pick", "product": product})
    return resp.get("data")


def update_status(product, query, status, notes=None):
    body = {"product": product, "query": query, "status": status}
    if notes is not None:
        body["notes"] = notes
    api_patch("/api/v1/seo/gsc-queries", body)


def summary(product):
    resp = api_get("/api/v1/seo/gsc-queries", query={"mode": "stats", "product": product})
    by_status = (resp.get("data") or {}).get("by_status") or []
    counts = {r.get("status"): int(r.get("count") or 0) for r in by_status}
    return "done={} pending={} skip={} in_progress={}".format(
        counts.get("done", 0), counts.get("pending", 0),
        counts.get("skip", 0), counts.get("in_progress", 0))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    product = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "pick":
        result = pick_next(product)
        print(json.dumps(result) if result else "")
    elif cmd == "mark":
        # mark <product> <query> <status> [notes]
        query = sys.argv[3]
        status = sys.argv[4]
        notes = sys.argv[5] if len(sys.argv) > 5 else None
        update_status(product, query, status, notes)
    elif cmd == "summary":
        print(summary(product))
    else:
        print("Usage: gsc_helpers.py <pick|mark|summary> <product> [query status notes]")
