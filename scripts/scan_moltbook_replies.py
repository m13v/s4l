#!/usr/bin/env python3
"""Scan Moltbook notifications for new replies to our content.

Uses /api/v1/notifications (inbox-style), mirroring the Reddit scanner.
Replaces the legacy per-post comment-polling scan, which broke when
/api/v1/posts/{uuid} stopped embedding the `comments` array and moved
them to /api/v1/posts/{uuid}/comments (~2026-03-18).

Handles notification types `comment_reply` and `mention`.
`dm_request` and `new_follower` are ignored (not engagement we reply to).

Inserts into the `replies` table as 'pending' (fresh) or 'skipped'
(backfill_old / too_short / deleted_or_spam). Matches posts by
`relatedPostId` against `posts.thread_url`. Dedupe key is
`relatedCommentId` via `reply_insert.already_tracked`.

Usage:
    python3 scripts/scan_moltbook_replies.py

Requires MOLTBOOK_API_KEY in ~/social-autoposter/.env.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, load_env
from reply_insert import insert_reply as _insert_reply
from moltbook_tools import (
    fetch_moltbook_json,
    HttpNotFoundError,
    MoltbookRateLimitedError,
)

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")

PAGE_LIMIT = 100
MAX_PAGES = 20  # caps pagination at ~2000 items per run
BACKFILL_HOURS = 48
CONSECUTIVE_KNOWN_STOP = 50
ENGAGE_TYPES = {"comment_reply", "mention"}
MIN_WORDS = 5
PAGE_PAUSE_SECONDS = 1.0


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def word_count(text):
    return len(text.split()) if text else 0


def parse_iso(ts):
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class MoltbookNotificationScanner:
    def __init__(self, api_key, self_username, self_agent_id, excluded_authors):
        self.api_key = api_key
        self.self_username_lower = (self_username or "").lower()
        self.self_agent_id = (self_agent_id or "").lower()
        self.excluded = {a.lower() for a in (excluded_authors or [])}
        self.excluded.update({"[deleted]", self.self_username_lower})
        # reply_insert.insert_reply ignores the `db` arg (HTTP-only) but still
        # accepts it positionally; pass None to satisfy the signature.
        self.db = None
        self.discovered = 0
        self.skipped_backfill = 0
        self.skipped_short = 0
        self.skipped_moderated = 0
        self.skipped_excluded = 0
        self.skipped_self = 0
        self.unmatched = 0
        self.total_seen = 0
        self.consecutive_known = 0

    def _post_id_for_moltbook(self, related_post_id):
        if not related_post_id:
            return None
        resp = api_get(
            "/api/v1/posts",
            query={
                "platform": "moltbook",
                "thread_url_contains": related_post_id,
                "order_by": "id",
                "order_dir": "desc",
                "limit": 1,
            },
        )
        rows = ((resp or {}).get("data") or {}).get("posts") or []
        return rows[0].get("id") if rows else None

    def _insert(self, post_id, comment_id, author, content, comment_url,
                post_uuid, status, skip_reason=None):
        counters_before = (self.discovered + self.skipped_backfill
                           + self.skipped_short + self.skipped_moderated)
        result = _insert_reply(
            self.db, post_id, "moltbook", comment_id, author, content, comment_url,
            status=status, skip_reason=skip_reason,
            moltbook_post_uuid=post_uuid,
        )
        if result == "pending":
            self.discovered += 1
        elif result == "skipped":
            if skip_reason == "backfill_old":
                self.skipped_backfill += 1
            elif skip_reason == "moderated":
                self.skipped_moderated += 1
            elif skip_reason and skip_reason.startswith("too_short"):
                self.skipped_short += 1
        counters_after = (self.discovered + self.skipped_backfill
                          + self.skipped_short + self.skipped_moderated)
        if counters_after == counters_before:
            self.consecutive_known += 1
        else:
            self.consecutive_known = 0

    def scan(self):
        if not self.api_key:
            print("MOLTBOOK_API_KEY not set, skipping Moltbook notification scan")
            return
        print("Scanning Moltbook notifications...")
        backfill_cutoff = datetime.now(timezone.utc).timestamp() - BACKFILL_HOURS * 3600
        cursor = None
        for page in range(1, MAX_PAGES + 1):
            url = f"https://www.moltbook.com/api/v1/notifications?limit={PAGE_LIMIT}"
            if cursor:
                url += f"&cursor={cursor}"
            try:
                data = fetch_moltbook_json(url, api_key=self.api_key)
            except MoltbookRateLimitedError as e:
                print(f"  Stopping scan: Moltbook rate-limited for {int(e.reset_seconds)}s")
                break
            except HttpNotFoundError:
                print("  Notifications endpoint returned 404; aborting scan")
                break
            if not data:
                print("  Empty response; aborting scan")
                break
            notifs = data.get("notifications") or []
            print(f"  page {page}: {len(notifs)} notifications (has_more={data.get('has_more')})")
            if not notifs:
                break
            for n in notifs:
                self.total_seen += 1
                ntype = n.get("type")
                if ntype not in ENGAGE_TYPES:
                    continue
                comment_id = n.get("relatedCommentId")
                post_uuid = n.get("relatedPostId")
                if not comment_id or not post_uuid:
                    continue
                post_id = self._post_id_for_moltbook(post_uuid)
                if not post_id:
                    self.unmatched += 1
                    continue
                comment = n.get("comment") or {}
                author_id = (comment.get("authorId") or "").strip()
                if author_id and author_id.lower() == self.self_agent_id:
                    self.skipped_self += 1
                    continue
                author = author_id or "[unknown]"
                if author.lower() in self.excluded:
                    self.skipped_excluded += 1
                    continue
                content = comment.get("content") or ""
                comment_url = f"https://www.moltbook.com/post/{post_uuid}#{comment_id}"

                if comment.get("isDeleted") or comment.get("isSpam") or comment.get("isFlagged"):
                    self._insert(post_id, comment_id, author, content, comment_url,
                                 post_uuid=post_uuid,
                                 status="skipped", skip_reason="moderated")
                    continue

                created_at = parse_iso(n.get("createdAt") or comment.get("createdAt"))
                is_old = bool(created_at and created_at.timestamp() < backfill_cutoff)

                if word_count(content) < MIN_WORDS:
                    self._insert(post_id, comment_id, author, content, comment_url,
                                 post_uuid=post_uuid,
                                 status="skipped",
                                 skip_reason=f"too_short ({word_count(content)} words)")
                elif is_old:
                    self._insert(post_id, comment_id, author, content, comment_url,
                                 post_uuid=post_uuid,
                                 status="skipped", skip_reason="backfill_old")
                else:
                    self._insert(post_id, comment_id, author, content, comment_url,
                                 post_uuid=post_uuid,
                                 status="pending")
                    print(f"  NEW: [{post_id}] author={author[:8]} {content[:80]}...")
                if self.consecutive_known >= CONSECUTIVE_KNOWN_STOP:
                    print(f"  hit {self.consecutive_known} consecutive already-known items, stopping pagination")
                    return
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
            if page < MAX_PAGES:
                time.sleep(PAGE_PAUSE_SECONDS)

    def finish(self):
        print(
            f"Notification scan complete: seen={self.total_seen} "
            f"new_pending={self.discovered} backfill_skipped={self.skipped_backfill} "
            f"too_short_skipped={self.skipped_short} moderated_skipped={self.skipped_moderated} "
            f"excluded_author={self.skipped_excluded} self_filtered={self.skipped_self} "
            f"unmatched_thread={self.unmatched}"
        )
        return {
            "discovered": self.discovered,
            "backfill_skipped": self.skipped_backfill,
            "too_short_skipped": self.skipped_short,
            "moderated_skipped": self.skipped_moderated,
            "excluded": self.skipped_excluded,
            "self_filtered": self.skipped_self,
            "unmatched": self.unmatched,
            "total_seen": self.total_seen,
        }


def main():
    load_env()
    api_key = os.environ.get("MOLTBOOK_API_KEY", "")
    config = load_config()
    acct = config.get("accounts", {}).get("moltbook", {}) or {}
    self_username = acct.get("username", "")
    self_agent_id = acct.get("agent_id", "")  # optional; filters own replies by agentId if set
    excluded_authors = config.get("exclusions", {}).get("authors", [])
    scanner = MoltbookNotificationScanner(api_key, self_username, self_agent_id, excluded_authors)
    scanner.scan()
    result = scanner.finish()
    sys.exit(0 if result["discovered"] > 0 else 1)


if __name__ == "__main__":
    main()
