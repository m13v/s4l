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
SEED = [
    (
        "i@m13v.com", True, [], "Matthew Diakonov",
        "Operator account (admin). Sees all projects, receives unscoped master daily report.",
    ),
    (
        "ethan@piastech.com", False, ["fde10x", "Assrt"], "Ethan",
        "Pre-existing client (provisioned 2026-04-21).",
    ),
    (
        "liam.collins@proxis.ai", False, ["c0nsl"], "Liam Collins",
        "Pre-existing client (provisioned 2026-04-21). uid 5Vf0uLKuDya3zS79u59EnUz9Pjw2.",
    ),
    (
        "kent@runner.now", False, ["Runner", "Agora", "Podlog"], "Kent Fenwick",
        "Founder of Runner/Agora/Podlog. Owns all three; one login, three projects.",
    ),
    (
        "gurbaz@getpieline.com", False, ["PieLine"], "Gurbaz Dhillon",
        "PieLine founder.",
    ),
    (
        "medewanouleonce@gmail.com", False, ["NightOwl"], "Leonce Medewanou",
        "NightOwl founder (github lemed99).",
    ),
    (
        "mustafa@capstacker.io", False, ["Capstacker"], "Mustafa Abbasoglu",
        "Capstacker founder (capstacker.io). Retainer $100/mo (provisioned 2026-06-25).",
    ),
]


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
