#!/usr/bin/env python3
"""Fetch pending Octolens mentions and output as thread candidates.

Two modes:
  1. --from-db: Read from octolens_mentions table (webhook-sourced)
  2. --from-api: Pull directly from Octolens API (no webhook needed)

Output format matches find_threads.py for compatibility with the posting flow.

Usage:
    python3 scripts/octolens_threads.py --from-db
    python3 scripts/octolens_threads.py --from-api --limit 20
    python3 scripts/octolens_threads.py --from-api --view "For you"
"""

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod
try:
    from account_resolver import resolve as _resolve_account
except Exception:
    def _resolve_account(_platform):  # type: ignore[unused-arg]
        return None

# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()
ENV_PATH = os.path.expanduser("~/social-autoposter/.env")

OCTOLENS_API_BASE = "https://app.octolens.com/api/v1"


def load_env():
    env = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                eq = line.index("=")
                key = line[:eq]
                val = line[eq + 1:].strip("\"'")
                env[key] = val
    except Exception:
        pass
    return env




def get_already_posted():
    """Return set of thread URLs we've already posted in.

    Octolens fans out across Twitter + Reddit mentions, so the dedupe set
    is the UNION of our threads on both platforms scoped to the configured
    handles. Falls back to the legacy global SELECT when no handles are
    resolvable, preserving single-account behavior for legacy callers.
    """
    twitter_acct = _resolve_account("twitter")
    reddit_acct = _resolve_account("reddit")
    conn = dbmod.get_conn()
    if twitter_acct or reddit_acct:
        clauses = []
        params = []
        if twitter_acct:
            clauses.append("(platform='twitter' AND our_account = %s)")
            params.append(twitter_acct)
        if reddit_acct:
            clauses.append("(platform='reddit'  AND our_account = %s)")
            params.append(reddit_acct)
        rows = conn.execute(
            "SELECT thread_url FROM posts "
            "WHERE thread_url IS NOT NULL AND (" + " OR ".join(clauses) + ")",
            params,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT thread_url FROM posts WHERE thread_url IS NOT NULL"
        ).fetchall()
    conn.close()
    return {row[0] for row in rows}


def get_already_processed():
    """Return set of octolens_ids already processed or skipped."""
    conn = dbmod.get_conn()
    rows = conn.execute(
        "SELECT octolens_id FROM octolens_mentions WHERE status != 'pending'"
    ).fetchall()
    conn.close()
    return {row[0] for row in rows}


def fetch_from_db(limit=50):
    """Read pending mentions from octolens_mentions table."""
    conn = dbmod.get_conn()
    rows = conn.execute(
        "SELECT id, octolens_id, platform, url, title, body, author, author_url, "
        "author_followers, sentiment, tags, keywords, source_timestamp "
        "FROM octolens_mentions WHERE status = 'pending' "
        "ORDER BY source_timestamp DESC LIMIT %s",
        [limit],
    ).fetchall()
    conn.close()

    mentions = []
    for row in rows:
        mentions.append({
            "db_id": row[0],
            "octolens_id": row[1],
            "platform": row[2],
            "url": row[3],
            "title": row[4] or "",
            "body": row[5] or "",
            "author": row[6] or "",
            "author_url": row[7] or "",
            "author_followers": row[8] or 0,
            "sentiment": row[9] or "",
            "tags": row[10] or "",
            "keywords": row[11] or "",
            "source_timestamp": str(row[12]) if row[12] else "",
        })
    return mentions


def fetch_from_api(api_key, limit=50, view_name=None):
    """Pull mentions directly from Octolens API."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "social-autoposter/1.0",
    }

    # Build request body
    body = {"limit": min(limit, 100)}

    # Use "For you" view by default (buy_intent, negative competitor, bug reports)
    if view_name:
        # Map known view names to IDs
        view_map = {
            "for you": 13153,
            "reddit": 16201,
            "linkedin": 13596,
            "twitter": 16554,
            "bluesky": 17024,
        }
        vid = view_map.get(view_name.lower())
        if vid:
            body["view"] = vid
    else:
        # Default: "For you" view
        body["view"] = 13153

    req = urllib.request.Request(
        f"{OCTOLENS_API_BASE}/mentions",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"ERROR fetching from Octolens API: {e}", file=sys.stderr)
        return []

    mentions = []
    for m in data.get("data", []):
        mentions.append({
            "octolens_id": m.get("id"),
            "platform": (m.get("source") or "unknown").replace("reddit_comment", "reddit"),
            "url": m.get("url", ""),
            "title": m.get("title", ""),
            "body": m.get("body", ""),
            "author": m.get("author", ""),
            "author_url": m.get("authorUrl", ""),
            "author_followers": m.get("authorFollowers", 0),
            "sentiment": m.get("sentiment", ""),
            "tags": ",".join(m.get("tags", [])),
            "keywords": ",".join(k.get("keyword", "") for k in m.get("keywords", [])),
            "source_timestamp": m.get("timestamp", ""),
        })
    return mentions


def mentions_to_candidates(mentions, already_posted):
    """Convert Octolens mentions to thread candidates matching find_threads.py format."""
    candidates = []
    config = load_config()
    exclusions = config.get("exclusions", {})
    excluded_authors = {a.lower() for a in exclusions.get("authors", [])}
    excluded_urls = set(exclusions.get("urls", []))

    for m in mentions:
        url = m["url"]

        # Skip already posted
        if url in already_posted:
            continue

        # Skip excluded authors
        if m["author"].lower() in excluded_authors:
            continue

        # Skip excluded URLs
        if any(exc in url for exc in excluded_urls):
            continue

        # Skip our own accounts
        accounts = config.get("accounts", {})
        our_handles = set()
        for acct in accounts.values():
            for key in ("username", "handle", "name"):
                if key in acct:
                    our_handles.add(acct[key].lower().lstrip("@"))
        if m["author"].lower().lstrip("@") in our_handles:
            continue

        candidate = {
            "platform": m["platform"],
            "url": url,
            "title": m["title"],
            "author": m["author"],
            "content": m["body"],
            "score": m.get("author_followers", 0),
            "num_comments": 0,
            "discovery_method": "octolens",
            "octolens_id": m.get("octolens_id"),
            "sentiment": m.get("sentiment", ""),
            "tags": m.get("tags", ""),
            "keywords": m.get("keywords", ""),
        }
        candidates.append(candidate)

    return candidates


def mark_processed(db_ids, status="processed", skip_reason=None):
    """Update status of processed mentions in DB."""
    if not db_ids:
        return
    conn = dbmod.get_conn()
    for db_id in db_ids:
        if skip_reason:
            conn.execute(
                "UPDATE octolens_mentions SET status = %s, processed_at = NOW(), skip_reason = %s WHERE id = %s",
                [status, skip_reason, db_id],
            )
        else:
            conn.execute(
                "UPDATE octolens_mentions SET status = %s, processed_at = NOW() WHERE id = %s",
                [status, db_id],
            )
    conn.conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Fetch Octolens mentions as thread candidates")
    parser.add_argument("--from-db", action="store_true", help="Read from octolens_mentions table")
    parser.add_argument("--from-api", action="store_true", help="Pull from Octolens API directly")
    parser.add_argument("--limit", type=int, default=50, help="Max mentions to fetch")
    parser.add_argument("--view", type=str, default=None, help="Octolens view name (default: For you)")
    parser.add_argument("--mark-skipped", action="store_true", help="Mark already-posted as skipped in DB")
    parser.add_argument("--platform", type=str, default=None, choices=["reddit", "twitter", "linkedin"],
                        help="Filter candidates to a single platform (reddit, twitter, linkedin)")
    args = parser.parse_args()

    if not args.from_db and not args.from_api:
        # Default: try DB first, fall back to API
        args.from_api = True

    env = load_env()
    already_posted = get_already_posted()

    if args.from_db:
        mentions = fetch_from_db(args.limit)
    else:
        api_key = env.get("OCTOLENS_API_KEY", os.environ.get("OCTOLENS_API_KEY", ""))
        if not api_key:
            print("ERROR: OCTOLENS_API_KEY not set in .env or environment", file=sys.stderr)
            sys.exit(1)
        mentions = fetch_from_api(api_key, args.limit, args.view)

    candidates = mentions_to_candidates(mentions, already_posted)

    # Filter by platform if requested. Octolens returns `source` values like
    # "reddit", "reddit_comment" (normalized to "reddit" above), "twitter",
    # "linkedin".
    if args.platform:
        candidates = [c for c in candidates if c.get("platform") == args.platform]

    # Output as JSON matching find_threads.py format
    output = {
        "source": "octolens",
        "platform": args.platform or "all",
        "total_mentions": len(mentions),
        "candidates": candidates,
        "already_posted_count": len(mentions) - len(candidates),
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
