#!/usr/bin/env python3
"""Ensure LinkedIn discovery tables carry the assigned search_topic.

The LinkedIn post-comments pipeline now mirrors Twitter's split between:

* search_topic: the project-level concept picked once per cycle
* search_query: the literal LinkedIn search phrase drafted from that topic

This helper keeps older installs from failing when the new columns are first
used by the pipeline.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


DDL = [
    """
    ALTER TABLE linkedin_candidates
        ADD COLUMN IF NOT EXISTS search_topic TEXT
    """,
    """
    ALTER TABLE linkedin_search_attempts
        ADD COLUMN IF NOT EXISTS search_topic TEXT
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_lc_search_topic
        ON linkedin_candidates(search_topic, discovered_at DESC)
        WHERE search_topic IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_lsa_search_topic
        ON linkedin_search_attempts(project_name, search_topic, ran_at DESC)
        WHERE search_topic IS NOT NULL
    """,
]


def ensure(conn):
    for sql in DDL:
        conn.execute(sql)
    conn.commit()


def main():
    conn = dbmod.get_conn()
    try:
        ensure(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
