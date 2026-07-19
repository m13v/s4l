#!/usr/bin/env python3
"""
seed_dashboard_users.py
Idempotent seed for dashboard_users. Inserts the admin row + the known external
clients. Uses ON CONFLICT (email) DO UPDATE so re-running is safe; existing
firebase_uid values are preserved (COALESCE).

Run after scripts/migrate_dashboard_users.sql has been applied.
"""

import os
import sys
from pathlib import Path

import psycopg2

_REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = _REPO_ROOT / ".env"
if ENV_FILE.exists():
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", file=sys.stderr)
    sys.exit(1)


# (email, admin, projects, name, notes)
# Project names MUST match config.json casing exactly. The auth layer matches
# claim values against posts.project_name via SQL IN, which is case-sensitive.
#
# The real client roster contains customer PII (names, emails, retainer terms),
# so it is NOT committed. It lives in the gitignored file
# `scripts/dashboard_users_seed.local.json` as a JSON array of
# [email, admin, projects, name, notes] rows. The placeholder below only
# documents the shape; populate the local JSON to seed real users.
import json

_SEED_LOCAL = Path(__file__).resolve().parent / "dashboard_users_seed.local.json"

_PLACEHOLDER_SEED = [
    (
        "admin@example.com", True, [], "Operator Name",
        "Admin account. Sees all projects, receives unscoped master daily report.",
    ),
    (
        "client@example.com", False, ["ProjectName"], "Client Name",
        "Example client row. Real roster lives in dashboard_users_seed.local.json.",
    ),
]

if _SEED_LOCAL.exists():
    SEED = [tuple(row) for row in json.loads(_SEED_LOCAL.read_text())]
else:
    print(
        f"WARNING: {_SEED_LOCAL.name} not found; using placeholder rows only. "
        "Create it (JSON array of [email, admin, projects, name, notes]) to seed real users.",
        file=sys.stderr,
    )
    SEED = _PLACEHOLDER_SEED


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    for email, admin, projects, name, notes in SEED:
        cur.execute(
            """
            INSERT INTO dashboard_users (email, admin, projects, name, notes, report_enabled)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (email) DO UPDATE
            SET admin = EXCLUDED.admin,
                projects = EXCLUDED.projects,
                name = COALESCE(EXCLUDED.name, dashboard_users.name),
                notes = COALESCE(EXCLUDED.notes, dashboard_users.notes)
            RETURNING (xmax = 0) AS inserted
            """,
            (email.lower(), admin, projects, name, notes),
        )
        inserted = cur.fetchone()[0]
        print(f"  {'INSERTED' if inserted else 'UPDATED '} {email:40s} "
              f"admin={admin}  projects={projects}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"\nSeed complete: {len(SEED)} rows.")


if __name__ == "__main__":
    main()
