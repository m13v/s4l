#!/usr/bin/env python3
"""One-shot migration: newsletter_links + newsletter_link_clicks.

The third short-link rail, after dm_links (Cal.com booking DMs) and
post_links (Reddit/Twitter/LinkedIn/GitHub public posts). Newsletter rail
attributes clicks from outbound broadcast emails sent by ~/analytics
(dash.m13v.com) back to the broadcast + recipient that triggered them.

Why a separate rail and not piggyback on post_links:
  - post_links inserts a `[CLICK_SIGNAL]` synthetic row into dm_messages
    for DM rail, and post_links has its own post_id/reply_id FK shape we
    don't need. Cleaner to keep concerns separate so future dashboard
    aggregations group cleanly by rail (post / dm / newsletter).
  - broadcast_id + recipient_email_hash are the natural FKs, not post_id.

Schema mirrors post_links shape:
  newsletter_links: one row per (broadcast x recipient x URL). Code is
    the short-link primary key. broadcast_id is opaque to S4L (analytics
    owns it; S4L just stores it). recipient_email_hash = sha256(lower(email))[:16]
    so we keep per-recipient attribution without storing raw emails.
  newsletter_link_clicks: one row per /r/<code> hit. Same is_bot split
    as post_link_clicks / dm_link_clicks.

Idempotent. Safe to re-run.

Run from repo root:
    python3 scripts/migrate_newsletter_links.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import load_env, get_conn


def main():
    load_env()
    conn = get_conn()

    statements = [
        # Parent table: one row per (broadcast x recipient x URL) mint.
        # broadcast_product + broadcast_id together identify the analytics-side
        # broadcast row (analytics has product-specific tables like
        # studyly_broadcast_log, fazm_broadcasts, etc., so the (product, id)
        # tuple is needed to disambiguate).
        """
        CREATE TABLE IF NOT EXISTS newsletter_links (
            code TEXT PRIMARY KEY,
            broadcast_product TEXT NOT NULL,
            broadcast_id BIGINT NOT NULL,
            recipient_email_hash TEXT NOT NULL,
            recipient_email TEXT,
            target_url TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'web',
            project_name TEXT,
            minted_at TIMESTAMP NOT NULL DEFAULT NOW(),
            clicks INTEGER NOT NULL DEFAULT 0,
            first_click_at TIMESTAMP,
            last_click_at TIMESTAMP,
            real_clicks INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_newsletter_links_broadcast ON newsletter_links(broadcast_product, broadcast_id)",
        "CREATE INDEX IF NOT EXISTS idx_newsletter_links_recipient ON newsletter_links(broadcast_product, broadcast_id, recipient_email_hash)",
        "CREATE INDEX IF NOT EXISTS idx_newsletter_links_project ON newsletter_links(project_name)",
        # Per-click log mirroring post_link_clicks shape.
        """
        CREATE TABLE IF NOT EXISTS newsletter_link_clicks (
            id BIGSERIAL PRIMARY KEY,
            code TEXT NOT NULL REFERENCES newsletter_links(code),
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ip_hash TEXT,
            user_agent TEXT,
            is_bot BOOLEAN NOT NULL DEFAULT FALSE,
            referrer TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS newsletter_link_clicks_code_ts ON newsletter_link_clicks(code, ts DESC)",
        "CREATE INDEX IF NOT EXISTS newsletter_link_clicks_is_bot ON newsletter_link_clicks(is_bot, ts DESC)",
    ]

    for s in statements:
        conn.execute(s)
        first_line = ' '.join(s.split())[:90]
        print("OK:", first_line)
    conn.commit()

    # Schema sanity print.
    for table in ('newsletter_links', 'newsletter_link_clicks'):
        cols = conn.execute(f"""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = '{table}'
            ORDER BY ordinal_position
        """).fetchall()
        print()
        print(f"{table} columns:")
        for c in cols:
            print(f"  {c['column_name']:<22} {c['data_type']:<28} nullable={c['is_nullable']}")
        counts = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        print(f"{table} rows: {counts['n']}")

    print()
    print("migration applied")


if __name__ == "__main__":
    main()
