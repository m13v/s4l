#!/usr/bin/env python3
"""One-off: list recent seo_page_improvements rows with assrt-tool detection."""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from pathlib import Path

env = Path(__file__).resolve().parent.parent / ".env"
for line in env.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor(cursor_factory=RealDictCursor)
cur.execute(
    """
  SELECT product, page_path, status, created_at, completed_at, tool_summary, run_log_path
  FROM seo_page_improvements
  WHERE created_at >= NOW() - INTERVAL '24 hours'
  ORDER BY created_at DESC
"""
)
for r in cur.fetchall():
    ts = r["tool_summary"] or {}
    has_assrt = any("assrt" in k.lower() for k in ts)
    print(
        f"{r['created_at'].strftime('%m-%d %H:%M')}  {r['status']:10s}  "
        f"{r['product']:22s}  {(r['page_path'] or '?'):40s}  "
        f"assrt_used={has_assrt}  tools={ts}"
    )
