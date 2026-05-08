#!/usr/bin/env python3
"""One-shot migration: per-click logging tables.

We discovered that the legacy `clicks` counter on post_links / dm_links is
inflated ~20x by Twitter card preview fetchers (Twitterbot, LinkedInBot,
Slackbot, etc.) hitting the /r/<code> resolver to generate link previews.
97% of links got their first click within 30s of mint, avg 17s. Real
human ratio is roughly 5-8% of total counts based on PostHog pageview
cross-reference.

Two new child tables capture per-click rows so we can split humans vs
bots after the fact (and keep historical accuracy going forward):

  post_link_clicks: one row per hit on /r/<code> for post_links.code
  dm_link_clicks:   one row per hit on /r/<code> for dm_links.code

The legacy `clicks` integer columns on post_links and dm_links are kept
intact (still incremented by the resolver for non-bot UAs), so existing
queries do not break. Dashboard surfaces both numbers side by side.

Idempotent. Safe to re-run.

Run from repo root:
    python3 scripts/migrate_link_clicks.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import load_env, get_conn


def main():
    load_env()
    conn = get_conn()

    statements = [
        # post rail per-click log. is_bot flips when User-Agent matches the
        # bot regex in the resolver (Twitterbot, LinkedInBot, Slackbot, etc.).
        # ip_hash is sha256(remote_ip)[:16] (hex), so we keep dedup capability
        # without storing raw IPs.
        """
        CREATE TABLE IF NOT EXISTS post_link_clicks (
            id BIGSERIAL PRIMARY KEY,
            code TEXT NOT NULL REFERENCES post_links(code),
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ip_hash TEXT,
            user_agent TEXT,
            is_bot BOOLEAN NOT NULL DEFAULT FALSE,
            referrer TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS post_link_clicks_code_ts ON post_link_clicks(code, ts DESC)",
        "CREATE INDEX IF NOT EXISTS post_link_clicks_is_bot ON post_link_clicks(is_bot, ts DESC)",
        # DM rail per-click log. Same shape; FK targets dm_links.code.
        """
        CREATE TABLE IF NOT EXISTS dm_link_clicks (
            id BIGSERIAL PRIMARY KEY,
            code TEXT NOT NULL REFERENCES dm_links(code),
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ip_hash TEXT,
            user_agent TEXT,
            is_bot BOOLEAN NOT NULL DEFAULT FALSE,
            referrer TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS dm_link_clicks_code_ts ON dm_link_clicks(code, ts DESC)",
        "CREATE INDEX IF NOT EXISTS dm_link_clicks_is_bot ON dm_link_clicks(is_bot, ts DESC)",
    ]

    for s in statements:
        conn.execute(s)
        first_line = ' '.join(s.split())[:90]
        print("OK:", first_line)
    conn.commit()

    # Schema sanity print.
    for table in ('post_link_clicks', 'dm_link_clicks'):
        cols = conn.execute(f"""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = '{table}'
            ORDER BY ordinal_position
        """).fetchall()
        print()
        print(f"{table} columns:")
        for c in cols:
            print(f"  {c['column_name']:<14} {c['data_type']:<28} nullable={c['is_nullable']}")
        counts = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        print(f"{table} rows: {counts['n']}")

    print()
    print("migration applied")


if __name__ == "__main__":
    main()
