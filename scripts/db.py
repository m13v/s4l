#!/usr/bin/env python3
"""Shared env loader for social-autoposter (clean, shipped).

The published package talks to the central store exclusively through the S4L
HTTP API (scripts/http_api.py). It ships NO direct database dependency: no
psycopg2, no DATABASE_URL requirement.

This module provides `load_env()` (the only DB-agnostic helper every pipeline
needs) and, for LOCAL operator installs only, re-exports the direct-Postgres
connection layer from `db_direct.py` when that file is present. `db_direct.py`
is excluded from the npm tarball, so on a clean install the direct-DB symbols
resolve to a hard-error stub instead of importing psycopg2.

.env is read from ~/social-autoposter/.env (pre-filled on install).
"""

import os

ENV_PATH = os.path.expanduser("~/social-autoposter/.env")


def load_env():
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())


# Re-export the direct-Postgres layer when running on a local operator install
# (db_direct.py present). In the published package db_direct.py is absent, so
# these names resolve to a stub that fails loudly if anything tries to open a
# direct DB connection — by design, the shipped pipelines use the HTTP API.
try:
    from db_direct import (  # noqa: F401
        get_conn,
        PGConn,
        snapshot_post_views,
    )
except ImportError:
    def _no_direct_db(*_args, **_kwargs):
        raise RuntimeError(
            "Direct database access is not available in this build. "
            "The published social-autoposter package uses the S4L HTTP API "
            "(scripts/http_api.py); set AUTOPOSTER_API_BASE in ~/social-autoposter/.env."
        )

    def get_conn(*_args, **_kwargs):  # noqa: F811
        return _no_direct_db()

    def snapshot_post_views(*_args, **_kwargs):  # noqa: F811
        # No-op stub: stats snapshots are an operator-local concern.
        return None

    PGConn = None
