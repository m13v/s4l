#!/usr/bin/env python3
"""Direct Postgres access for social-autoposter (LOCAL OPERATOR USE ONLY).

This module is intentionally EXCLUDED from the published npm package. It holds
the psycopg2-backed connection layer used by the local dashboard / operator
scripts that talk straight to the central database. The shipped pipelines all
go through the S4L HTTP API (scripts/http_api.py) and never import this file.

db.py re-exports get_conn / PGConn / snapshot_post_views from here when present,
and falls back to a hard-error stub when it is absent (i.e. in the clean
published tarball).

DATABASE_URL is read from ~/social-autoposter/.env.
"""

import os
import re
import sys

from db import load_env  # clean, always-shipped helper


def _translate_sql(sql):
    """Translate SQLite-specific SQL syntax to PostgreSQL."""
    # ? placeholders -> %s
    sql = sql.replace('?', '%s')
    # datetime('now', '-N hours') -> NOW() - INTERVAL 'N hours'
    sql = re.sub(r"datetime\('now',\s*'-(\d+) hours'\)", r"NOW() - INTERVAL '\1 hours'", sql)
    # datetime('now', '-N days') -> NOW() - INTERVAL 'N days'
    sql = re.sub(r"datetime\('now',\s*'-(\d+) days'\)", r"NOW() - INTERVAL '\1 days'", sql)
    # datetime('now') -> NOW()
    sql = re.sub(r"datetime\('now'\)", 'NOW()', sql)
    # status_checked_at=datetime('now') already handled above
    return sql


_DNS_TRANSIENT_MARKERS = (
    "could not translate host name",
    "nodename nor servname provided",
    "Temporary failure in name resolution",
    "Name or service not known",
)


def _connect_with_retry(url):
    """psycopg2.connect with 3-try backoff on DNS resolution failures.

    Transient DNS blips from the local resolver (observed ~40 min on
    2026-04-23 PDT) crashed every tick that happened to run during the
    window. Retry only on name-resolution OperationalError messages so
    auth or hard-down errors fail fast.
    """
    import time
    import psycopg2
    backoffs = (1.0, 2.0)
    last_exc = None
    for attempt in range(3):
        try:
            return psycopg2.connect(url, keepalives=1,
                                    keepalives_idle=30,
                                    keepalives_interval=10,
                                    keepalives_count=5)
        except psycopg2.OperationalError as exc:
            msg = str(exc)
            if not any(m in msg for m in _DNS_TRANSIENT_MARKERS):
                raise
            last_exc = exc
            if attempt < len(backoffs):
                print(f"[db] DNS transient, retrying in {backoffs[attempt]}s: {msg.strip()}",
                      file=sys.stderr)
                time.sleep(backoffs[attempt])
    raise last_exc


class PGConn:
    """Thin psycopg2 wrapper with a sqlite3-compatible execute/commit/close API."""

    def __init__(self, conn, url=None):
        import psycopg2.extras
        self._conn = conn
        self._url = url
        self._cursor_factory = psycopg2.extras.DictCursor
        self._apply_session_defaults()

    def _apply_session_defaults(self):
        # 5 min per-statement ceiling. Prevents silent hangs when the
        # pooler drops an idle socket and psycopg2's recv stalls forever
        # (originally observed 2026-04-23/24 on Neon under stats.py
        # --github-only; kept after Cloud SQL migration as defensive). The
        # pooler rejects libpq `options=` at connect time, so we set this via
        # SQL after connect. Reapplied after commit() because transaction-mode
        # pooling resets session state on COMMIT.
        try:
            cur = self._conn.cursor()
            cur.execute("SET statement_timeout = 300000")
            cur.close()
        except Exception:
            pass

    def _reconnect(self):
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = _connect_with_retry(self._url)
        self._apply_session_defaults()

    def execute(self, sql, params=None):
        import psycopg2
        sql = _translate_sql(sql)
        try:
            cur = self._conn.cursor(cursor_factory=self._cursor_factory)
            if params is not None:
                cur.execute(sql, list(params))
            else:
                cur.execute(sql)
            return cur
        except psycopg2.OperationalError:
            self._reconnect()
            cur = self._conn.cursor(cursor_factory=self._cursor_factory)
            if params is not None:
                cur.execute(sql, list(params))
            else:
                cur.execute(sql)
            return cur

    def commit(self):
        import psycopg2
        try:
            self._conn.commit()
        except psycopg2.OperationalError:
            self._reconnect()
            self._conn.commit()
        # The transaction-mode pooler resets session state on COMMIT, so
        # re-apply statement_timeout for the next transaction.
        self._apply_session_defaults()

    def close(self):
        self._conn.close()

    # No-op to absorb sqlite3.Row assignments
    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, val):
        pass


def snapshot_post_views(db, post_id, views):
    """UPSERT one row of post_views_daily with today's view/upvote/comment count.

    Called from the Reddit + Twitter refresh jobs whenever a fresh view count
    is observed for a post. Later observations on the same day overwrite the
    earlier ones so end-of-day has the final number. The dashboard computes
    daily deltas with LAG() over (post_id ORDER BY day) to render gains
    earned on day D across all posts.

    Upvotes and comments are read from posts.upvotes / posts.comments_count at
    write time. Callers UPDATE those columns on the post row immediately
    before calling this function, so the subselects pick up the fresh values
    without requiring each caller to pass them explicitly.
    """
    if post_id is None or views is None:
        return
    try:
        db.execute(
            "INSERT INTO post_views_daily (post_id, day, views, upvotes, comments, captured_at) "
            "VALUES (%s, CURRENT_DATE, %s, "
            "  (SELECT upvotes FROM posts WHERE id = %s), "
            "  (SELECT comments_count FROM posts WHERE id = %s), "
            "  NOW()) "
            "ON CONFLICT (post_id, day) DO UPDATE SET "
            "  views = EXCLUDED.views, "
            "  upvotes = EXCLUDED.upvotes, "
            "  comments = EXCLUDED.comments, "
            "  captured_at = EXCLUDED.captured_at",
            [post_id, int(views), post_id, post_id],
        )
    except Exception:
        pass


def get_conn():
    """Return a PGConn connected to the central Postgres database."""
    load_env()
    url = os.environ.get('DATABASE_URL')
    if not url:
        print("ERROR: DATABASE_URL not set in ~/social-autoposter/.env", file=sys.stderr)
        print("  Re-run: npx social-autoposter init", file=sys.stderr)
        sys.exit(1)
    try:
        import psycopg2  # noqa: F401 — verifies availability before retry helper imports it
    except ImportError:
        print("ERROR: psycopg2-binary not installed.", file=sys.stderr)
        print("  Run: pip3 install psycopg2-binary", file=sys.stderr)
        sys.exit(1)
    conn = _connect_with_retry(url)
    return PGConn(conn, url=url)
