#!/usr/bin/env python3
"""Scan Instagram Graph API for new comments on our posts.

For each enabled Instagram account in config.json (matt_diak, matthewheartful,
omidotme), this:

  1. Fetches /api/v1/posts?platform=instagram&our_account=<username> to build
     a {shortcode: post_id} map of our DB-tracked IG posts.
  2. Lists /me/media for the account (reuses the same Graph API call shape
     update_instagram_stats.py uses).
  3. For each media item present in our DB, calls /{media-id}/comments with
     the replies sub-resource expanded.
  4. Inserts each comment (and its nested replies) into the `replies` table
     via reply_insert.insert_reply(). Server-side UNIQUE (platform,
     their_comment_id) handles dedup; this script never SELECTs.

Filters (mirrors scan_reddit_replies / scan_github_replies behavior):
  - Skip comments whose author is in config.exclusions.authors
  - Skip our own usernames (matt_diak / matthewheartful / omidotme) so we
    don't try to reply to ourselves
  - Skip backfill-old comments (older than BACKFILL_HOURS) with
    status='skipped' / skip_reason='backfill_old'
  - Skip too-short comments (< MIN_WORDS) with skip_reason='too_short'

This is discovery-only. Posting replies back to Instagram lives in a separate
engage script (Phase 2, not built yet); for now new rows surface in the
dashboard replies feed as platform='instagram', status='pending'.

Usage:
    python3 scripts/scan_instagram_comments.py [--quiet] [--limit N]
                                               [--account NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get
from reply_insert import insert_reply as _insert_reply


IG_ENV_PATH = Path.home() / "instagram-graph-api" / ".env"
GRAPH = "https://graph.instagram.com/v22.0"
SA_CONFIG = Path(__file__).resolve().parent.parent / "config.json"

# Discovery filters
BACKFILL_HOURS = 48
MIN_WORDS = 5
# Per-Graph-API-call sleep so we stay polite under the 60/hr, 4800/day caps.
# 3 accounts * ~10 media * (1 list + 1 comments call) = ~60 calls/cycle;
# at 0.2s sleep that's ~12s per cycle, well inside 30-minute scheduling.
GRAPH_SLEEP_SECS = 0.2


# ── env / config ──────────────────────────────────────────────────────────────

def load_ig_env() -> dict:
    if not IG_ENV_PATH.exists():
        return {}
    env = {}
    for line in IG_ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def load_config() -> dict:
    try:
        return json.loads(SA_CONFIG.read_text())
    except FileNotFoundError:
        return {}


def resolve_account_creds(account_name: str, ig_env: dict, accounts_cfg: list):
    """Return (ig_user_id, long_token) or (None, None). Matches the lookup
    pattern in scripts/update_instagram_stats.py."""
    match = next(
        (a for a in accounts_cfg if a.get("username", "").lower() == account_name.lower()),
        None,
    )
    if match:
        uid = ig_env.get(match.get("ig_user_id_env", "IG_USER_ID"))
        tok = ig_env.get(match.get("ig_long_token_env", "IG_LONG_TOKEN"))
        if uid and tok:
            return uid, tok
    uid = ig_env.get("IG_USER_ID")
    tok = ig_env.get("IG_LONG_TOKEN")
    return uid, tok


# ── Graph API helpers ─────────────────────────────────────────────────────────

def graph_get(path: str, token: str, **params):
    params["access_token"] = token
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def shortcode_from_url(url: str | None) -> str | None:
    """Extract shortcode from an IG permalink.

    https://www.instagram.com/reel/DYkkj8RDo9P/ -> DYkkj8RDo9P
    """
    import re
    m = re.search(r"/(?:reel|p|tv)/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else None


def fetch_media_list(ig_user_id: str, token: str, max_pages: int = 5) -> list[dict]:
    """Page through /me/media. Returns the raw items list with permalink + id."""
    out = []
    fields = "id,media_type,media_product_type,permalink,timestamp"
    url = (
        f"{GRAPH}/{ig_user_id}/media"
        f"?fields={fields}&limit=100&access_token={token}"
    )
    pages = 0
    while url and pages < max_pages:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
        out.extend(data.get("data", []) or [])
        url = (data.get("paging") or {}).get("next")
        pages += 1
        if url:
            time.sleep(GRAPH_SLEEP_SECS)
    return out


def fetch_comments(media_id: str, token: str) -> list[dict]:
    """Return top-level comments for a media item, each with a nested
    `replies.data[]` list (Graph API caps the sub-list at 25 by default; that
    matches typical traffic on our posts)."""
    fields = (
        "id,username,text,timestamp,"
        "replies{id,username,text,timestamp}"
    )
    try:
        data = graph_get(f"{media_id}/comments", token, fields=fields, limit=50)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        raise GraphApiError(f"HTTP {e.code} on /{media_id}/comments: {body}")
    return data.get("data", []) or []


class GraphApiError(Exception):
    pass


# ── posts lookup ──────────────────────────────────────────────────────────────

def fetch_posts_map(account_username: str) -> dict[str, int]:
    """Build {shortcode: post_id} for posts.platform='instagram' AND
    posts.our_account=account_username. Uses the same /api/v1/posts endpoint
    scan_reddit_replies.py uses for its post-id lookup."""
    out: dict[str, int] = {}
    resp = api_get(
        "/api/v1/posts",
        query={"platform": "instagram", "limit": 500},
    )
    posts = ((resp or {}).get("data") or {}).get("posts") or []
    for p in posts:
        if (p.get("our_account") or "").lower() != account_username.lower():
            continue
        code = shortcode_from_url(p.get("our_url"))
        if code:
            out[code] = int(p.get("id"))
    return out


# ── parse / classify ──────────────────────────────────────────────────────────

def parse_ts(ts: str | None) -> float:
    """Parse an IG ISO-8601 timestamp to a unix timestamp. Returns 0 on
    failure (which counts as "old" for backfill purposes)."""
    if not ts:
        return 0.0
    try:
        # Instagram returns +0000 (no colon), strip and parse as UTC.
        s = ts.replace("+0000", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def word_count(text: str | None) -> int:
    return len((text or "").split())


def build_comment_url(shortcode: str, comment_id: str) -> str:
    return f"https://www.instagram.com/p/{shortcode}/c/{comment_id}/"


# ── main scan loop ────────────────────────────────────────────────────────────

class IgCommentScanner:
    def __init__(
        self,
        account_username: str,
        ig_user_id: str,
        token: str,
        posts_map: dict[str, int],
        excluded_authors: set[str],
        quiet: bool = False,
        media_limit: int | None = None,
    ):
        self.account = account_username
        self.ig_user_id = ig_user_id
        self.token = token
        self.posts_map = posts_map
        self.excluded = excluded_authors
        self.quiet = quiet
        self.media_limit = media_limit

        self.discovered = 0
        self.backfill_skipped = 0
        self.too_short_skipped = 0
        self.excluded_skipped = 0
        self.already_tracked = 0
        self.media_checked = 0
        self.media_no_post = 0
        self.comments_seen = 0

    def log(self, msg: str):
        if not self.quiet:
            print(msg)

    def _insert(
        self,
        post_id: int,
        comment_id: str,
        author: str,
        content: str,
        comment_url: str,
        depth: int,
        status: str,
        skip_reason: str | None = None,
    ):
        result = _insert_reply(
            None, post_id, "instagram", comment_id, author, content, comment_url,
            parent_reply_id=None, depth=depth, status=status, skip_reason=skip_reason,
        )
        if result is None:
            self.already_tracked += 1
            return
        if result == "pending":
            self.discovered += 1
        elif result == "skipped":
            if skip_reason == "backfill_old":
                self.backfill_skipped += 1
            elif skip_reason and skip_reason.startswith("too_short"):
                self.too_short_skipped += 1
            elif skip_reason == "excluded_author":
                self.excluded_skipped += 1

    def _classify_and_insert(
        self,
        post_id: int,
        shortcode: str,
        comment: dict,
        backfill_cutoff: float,
        depth: int,
    ):
        comment_id = str(comment.get("id") or "")
        if not comment_id:
            return
        self.comments_seen += 1
        author = comment.get("username") or ""
        content = comment.get("text") or ""
        comment_url = build_comment_url(shortcode, comment_id)
        created = parse_ts(comment.get("timestamp"))

        if author.lower() in self.excluded:
            self._insert(
                post_id, comment_id, author, content, comment_url, depth,
                status="skipped", skip_reason="excluded_author",
            )
            return

        if created and created < backfill_cutoff:
            self._insert(
                post_id, comment_id, author, content, comment_url, depth,
                status="skipped", skip_reason="backfill_old",
            )
            return

        wc = word_count(content)
        if wc < MIN_WORDS:
            self._insert(
                post_id, comment_id, author, content, comment_url, depth,
                status="skipped", skip_reason=f"too_short ({wc} words)",
            )
            return

        self._insert(
            post_id, comment_id, author, content, comment_url, depth,
            status="pending", skip_reason=None,
        )

    def scan(self):
        self.log(f"[scan-ig-comments] account={self.account} posts_in_db={len(self.posts_map)}")
        if not self.posts_map:
            self.log(f"[scan-ig-comments]   no instagram posts in DB for account={self.account}; nothing to scan")
            return

        try:
            media_items = fetch_media_list(self.ig_user_id, self.token)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            self.log(f"[scan-ig-comments] /me/media failed for {self.account}: HTTP {e.code} {body}")
            return
        except Exception as e:
            self.log(f"[scan-ig-comments] /me/media failed for {self.account}: {e}")
            return

        self.log(f"[scan-ig-comments]   /me/media returned {len(media_items)} items")
        backfill_cutoff = time.time() - BACKFILL_HOURS * 3600

        checked = 0
        for item in media_items:
            if self.media_limit and checked >= self.media_limit:
                break
            permalink = item.get("permalink")
            shortcode = shortcode_from_url(permalink)
            if not shortcode:
                continue
            post_id = self.posts_map.get(shortcode)
            if not post_id:
                self.media_no_post += 1
                continue

            media_id = item.get("id")
            try:
                comments = fetch_comments(media_id, self.token)
            except GraphApiError as e:
                self.log(f"[scan-ig-comments]   media={media_id} shortcode={shortcode} comments fetch failed: {e}")
                continue

            self.media_checked += 1
            checked += 1
            self.log(
                f"[scan-ig-comments]   media={media_id} shortcode={shortcode} "
                f"top_level_comments={len(comments)}"
            )

            for c in comments:
                self._classify_and_insert(post_id, shortcode, c, backfill_cutoff, depth=1)
                # Nested replies (replies to top-level comments). Author may
                # be us (we already replied) or someone else (we got a reply
                # to OUR reply). The excluded-author filter inside
                # _classify_and_insert handles the first case.
                replies = ((c.get("replies") or {}).get("data") or [])
                for r in replies:
                    self._classify_and_insert(post_id, shortcode, r, backfill_cutoff, depth=2)

            time.sleep(GRAPH_SLEEP_SECS)

    def summary(self) -> dict:
        return {
            "account": self.account,
            "media_checked": self.media_checked,
            "media_no_post_in_db": self.media_no_post,
            "comments_seen": self.comments_seen,
            "discovered": self.discovered,
            "backfill_skipped": self.backfill_skipped,
            "too_short_skipped": self.too_short_skipped,
            "excluded_skipped": self.excluded_skipped,
            "already_tracked": self.already_tracked,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap media items inspected per account (debug)")
    parser.add_argument("--account", default=None,
                        help="Scan only this account (default: all enabled)")
    args = parser.parse_args()

    ig_env = load_ig_env()
    cfg = load_config()
    accounts_cfg = ((cfg.get("instagram") or {}).get("accounts") or [])
    exclusions = cfg.get("exclusions") or {}
    base_excluded = {a.lower() for a in (exclusions.get("authors") or [])}
    # Always exclude our own usernames so we don't reply to ourselves.
    own_usernames = {a.get("username", "").lower() for a in accounts_cfg if a.get("username")}

    if args.account:
        accounts_to_scan = [a for a in accounts_cfg
                            if a.get("username", "").lower() == args.account.lower()]
    else:
        accounts_to_scan = [a for a in accounts_cfg if a.get("enabled", True)]

    if not accounts_to_scan:
        print("[scan-ig-comments] no instagram accounts to scan; exiting")
        print("SUMMARY:DISCOVERED=0 SKIPPED=0 CHECKED=0 ALREADY=0 ACCOUNTS=0")
        return

    totals = {
        "discovered": 0,
        "backfill_skipped": 0,
        "too_short_skipped": 0,
        "excluded_skipped": 0,
        "already_tracked": 0,
        "media_checked": 0,
        "comments_seen": 0,
        "accounts": 0,
    }

    for account_cfg in accounts_to_scan:
        username = account_cfg.get("username", "")
        if not username:
            continue
        uid, tok = resolve_account_creds(username, ig_env, accounts_cfg)
        if not uid or not tok:
            print(f"[scan-ig-comments] missing creds for account={username}; skipping")
            continue

        excluded_for_account = set(base_excluded) | set(own_usernames)

        try:
            posts_map = fetch_posts_map(username)
        except Exception as e:
            print(f"[scan-ig-comments] posts lookup failed for {username}: {e}")
            continue

        scanner = IgCommentScanner(
            username, uid, tok, posts_map, excluded_for_account,
            quiet=args.quiet, media_limit=args.limit,
        )
        scanner.scan()
        s = scanner.summary()
        if not args.quiet:
            print(
                f"[scan-ig-comments] account={username} done: "
                f"media_checked={s['media_checked']} comments_seen={s['comments_seen']} "
                f"discovered={s['discovered']} "
                f"backfill_skipped={s['backfill_skipped']} "
                f"too_short_skipped={s['too_short_skipped']} "
                f"excluded_skipped={s['excluded_skipped']} "
                f"already_tracked={s['already_tracked']}"
            )

        totals["discovered"] += s["discovered"]
        totals["backfill_skipped"] += s["backfill_skipped"]
        totals["too_short_skipped"] += s["too_short_skipped"]
        totals["excluded_skipped"] += s["excluded_skipped"]
        totals["already_tracked"] += s["already_tracked"]
        totals["media_checked"] += s["media_checked"]
        totals["comments_seen"] += s["comments_seen"]
        totals["accounts"] += 1

    skipped_total = (
        totals["backfill_skipped"]
        + totals["too_short_skipped"]
        + totals["excluded_skipped"]
    )

    print(
        f"SUMMARY:DISCOVERED={totals['discovered']} SKIPPED={skipped_total} "
        f"CHECKED={totals['media_checked']} ALREADY={totals['already_tracked']} "
        f"ACCOUNTS={totals['accounts']}"
    )


if __name__ == "__main__":
    main()
