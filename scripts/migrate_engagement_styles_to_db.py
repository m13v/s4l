#!/usr/bin/env python3
"""One-shot migration: create engagement_styles_registry and seed it from
the hardcoded STYLES dict + the legacy scripts/engagement_styles_extra.json
sidecar. Idempotent: re-runs are safe.

After this runs successfully and engagement_styles.py is flipped to read
from the DB, delete the sidecar JSON + lock file (handled by a separate
cleanup step, not here).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_conn  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS engagement_styles_registry (
  name                    TEXT PRIMARY KEY,
  description             TEXT NOT NULL,
  example                 TEXT NOT NULL DEFAULT '',
  note                    TEXT NOT NULL DEFAULT '',
  best_in                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  status                  TEXT NOT NULL DEFAULT 'active',
  why_existing_didnt_fit  TEXT,
  first_post_url          TEXT,
  first_post_id           INTEGER,
  first_post_platform     TEXT,
  invented_by_model       TEXT,
  invented_at             TIMESTAMPTZ,
  promoted_at             TIMESTAMPTZ,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_engagement_styles_registry_status
  ON engagement_styles_registry (status);
"""


def main():
    from engagement_styles import STYLES
    sidecar_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "engagement_styles_extra.json",
    )
    sidecar = {}
    if os.path.exists(sidecar_path):
        with open(sidecar_path) as f:
            sidecar = json.load(f)

    c = get_conn()
    c.execute(DDL)
    c.execute(INDEX_DDL)
    c.commit()
    print("[migration] table + index ready")

    inserted_seed = 0
    inserted_sidecar = 0

    for name, meta in STYLES.items():
        cur = c.execute(
            """
            INSERT INTO engagement_styles_registry
              (name, description, example, note, best_in, status)
            VALUES (%s, %s, %s, %s, %s::jsonb, 'active')
            ON CONFLICT (name) DO NOTHING
            """,
            [
                name,
                meta.get("description", ""),
                meta.get("example", ""),
                meta.get("note", ""),
                json.dumps(meta.get("best_in", {})),
            ],
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted_seed += 1
    c.commit()
    print(f"[migration] seeded {inserted_seed} hardcoded STYLES (skipped existing)")

    for name, meta in sidecar.items():
        if not isinstance(meta, dict):
            continue
        cur = c.execute(
            """
            INSERT INTO engagement_styles_registry
              (name, description, example, note, best_in, status,
               why_existing_didnt_fit, first_post_url, first_post_id,
               first_post_platform, invented_by_model, invented_at, promoted_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s)
            ON CONFLICT (name) DO NOTHING
            """,
            [
                name,
                meta.get("description", ""),
                meta.get("example", ""),
                meta.get("note", ""),
                json.dumps(meta.get("best_in", {})),
                meta.get("status", "candidate"),
                meta.get("why_existing_didnt_fit"),
                meta.get("first_post_url"),
                meta.get("first_post_id"),
                meta.get("first_post_platform"),
                meta.get("invented_by_model"),
                meta.get("invented_at"),
                meta.get("promoted_at"),
            ],
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted_sidecar += 1
    c.commit()
    print(f"[migration] seeded {inserted_sidecar} sidecar entries (skipped existing)")

    cur = c.execute("SELECT name, status FROM engagement_styles_registry ORDER BY name")
    rows = cur.fetchall()
    print(f"[migration] total rows: {len(rows)}")
    for r in rows:
        print(f"  - {r[0]:30s} status={r[1]}")

    c.close()


if __name__ == "__main__":
    main()
