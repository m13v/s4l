#!/usr/bin/env python3
"""LinkedIn discovery search_topic columns: now a graceful no-op.

The LinkedIn post-comments pipeline mirrors Twitter's split between:

* search_topic: the project-level concept picked once per cycle
* search_query: the literal LinkedIn search phrase drafted from that topic

These columns (linkedin_candidates.search_topic, linkedin_search_attempts.
search_topic) and their indexes now exist permanently in the production
schema, which is authoritative. As of the 2026-06-01 HTTP-API migration this
process no longer holds a DATABASE_URL, so there is nothing for it to migrate.

We keep the DDL below as documentation of the expected shape, but ensure() is
now a no-op so the run-linkedin.sh callsites (standalone invocation + the
PA_PICK import) keep working without any direct DB access.
"""

import sys

# Documented expected schema. Applied once, server-side, when these columns
# were first introduced; retained here only as a reference of the shape.
DDL = [
    "ALTER TABLE linkedin_candidates "
    "ADD COLUMN IF NOT EXISTS search_topic TEXT",
    "ALTER TABLE linkedin_search_attempts "
    "ADD COLUMN IF NOT EXISTS search_topic TEXT",
    "CREATE INDEX IF NOT EXISTS idx_lc_search_topic "
    "ON linkedin_candidates(search_topic, discovered_at DESC) "
    "WHERE search_topic IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_lsa_search_topic "
    "ON linkedin_search_attempts(project_name, search_topic, ran_at DESC) "
    "WHERE search_topic IS NOT NULL",
]


def ensure(conn=None):
    """No-op. The production schema is authoritative; the search_topic
    columns and indexes already exist. Accepts an optional conn argument
    for backward compatibility with old callers, and ignores it."""
    return None


def main():
    return 0


if __name__ == "__main__":
    sys.exit(main())
