#!/usr/bin/env python3
"""Scan Reddit inbox for new replies, then engage up to N of them.

Replaces the legacy per-post anonymous scan that was rate-limited by
old.reddit.com. Reads /message/inbox/.json with the logged-in
reddit-agent profile cookies (refreshed by bootstrap_reddit_cookies.py),
inserts new rows into `replies`, and immediately fires engage_reddit.py
with --limit so the loop runs end-to-end every 5 min.

Inbox cannot tell us depth/parent_reply_id (it shows comment-replies and
post-replies identically). We insert depth=1 / parent_reply_id=NULL; the
engage step reads the live thread URL anyway.

Items older than BACKFILL_HOURS that aren't already in the DB are marked
status='skipped' / skip_reason='backfill_old' so they show in the
dashboard without being responded to.

Usage:
    python3 scripts/scan_reddit_replies.py [--reddit-account NAME]
                                           [--engage-limit N]
                                           [--no-engage]
                                           [--no-jitter]
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get
from reply_insert import insert_reply as _insert_reply

# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()
COOKIES_PATH = os.path.expanduser("~/.config/social-autoposter/reddit-cookies.json")
ENGAGE_SCRIPT = os.path.expanduser("~/social-autoposter/scripts/engage_reddit.py")

INBOX_URL = "https://old.reddit.com/message/inbox/.json"
PAGE_LIMIT = 100
MAX_PAGES = 10  # caps pagination at ~1000 items; inbox retention is shorter than that anyway
BACKFILL_HOURS = 48
JITTER_MAX_SECS = 60
PAGE_PAUSE_SECS = 1.5
OWN_COMMENTS_PAGES = 20  # hard cap on pagination depth (max 2000 items)
OWN_COMMENTS_LOOKBACK_DAYS = 30  # stop once we pass this many days back

THREAD_ID_RE = re.compile(r"/comments/([a-z0-9]+)/")




def load_cookies():
    if not os.path.exists(COOKIES_PATH):
        return None
    with open(COOKIES_PATH) as f:
        cookies = json.load(f)
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def fetch_inbox(cookie_header, user_agent, after=None):
    url = f"{INBOX_URL}?limit={PAGE_LIMIT}"
    if after:
        url += f"&after={after}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Cookie": cookie_header,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        ct = resp.headers.get("Content-Type", "")
        if "application/json" not in ct:
            raise SessionInvalidError(f"non-JSON response (likely login redirect): {ct}")
        data = json.loads(resp.read())
    if data.get("kind") != "Listing":
        raise SessionInvalidError(f"unexpected kind: {data.get('kind')}")
    return data["data"]


class SessionInvalidError(Exception):
    pass


def fetch_own_replies(reddit_account, cookie_header, user_agent,
                       pages=OWN_COMMENTS_PAGES, lookback_days=OWN_COMMENTS_LOOKBACK_DAYS):
    """Build {parent_comment_id: {reply_id, reply_url, reply_content, replied_at}}
    by paging /user/<account>/comments.json. Used to detect comments the account
    already replied to outside the pipeline (e.g., manual browser replies).
    Stops when a page's oldest comment is older than lookback_days, or after
    `pages` pages, whichever comes first."""
    out = {}
    after = None
    cutoff = time.time() - lookback_days * 86400
    url_base = f"https://old.reddit.com/user/{reddit_account}/comments/.json?limit={PAGE_LIMIT}"
    for page in range(pages):
        url = url_base + (f"&after={after}" if after else "")
        req = urllib.request.Request(url, headers={
            "User-Agent": user_agent, "Cookie": cookie_header, "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if "application/json" not in resp.headers.get("Content-Type", ""):
                    return out  # non-fatal; just skip the map
                data = json.loads(resp.read()).get("data", {})
        except Exception as e:
            print(f"  own-replies fetch failed on page {page+1}: {e}")
            return out
        children = data.get("children", []) or []
        oldest_on_page = 0
        for c in children:
            d = c.get("data") or {}
            created = float(d.get("created_utc") or 0)
            if created and (oldest_on_page == 0 or created < oldest_on_page):
                oldest_on_page = created
            parent = (d.get("parent_id") or "")
            if not parent.startswith("t1_"):
                continue  # only comment-parents; post-parents handled via inbox matching
            parent_id = parent.removeprefix("t1_")
            if parent_id in out:
                continue
            reply_id = d.get("id")
            permalink = d.get("permalink")
            out[parent_id] = {
                "our_reply_id": reply_id,
                "our_reply_url": f"https://old.reddit.com{permalink}" if permalink else None,
                "our_reply_content": d.get("body") or "",
                "replied_at": created or None,
            }
        after = data.get("after")
        if not after:
            break
        if oldest_on_page and oldest_on_page < cutoff:
            break  # we've reached lookback horizon
        time.sleep(PAGE_PAUSE_SECS)
    return out


class InboxScanner:
    def __init__(self, reddit_account, user_agent, cookie_header, excluded_authors=None,
                 own_replies_map=None):
        # No DB handle anymore — every read/write hits the API. The `db` field
        # is kept for back-compat with `_insert_reply(self.db, ...)` callers
        # (they pass it through unchanged); the helper itself ignores the value.
        self.db = None
        self.reddit_account = reddit_account
        self.reddit_account_lower = reddit_account.lower()
        self.user_agent = user_agent
        self.cookie_header = cookie_header
        self.excluded = {a.lower() for a in (excluded_authors or set())}
        self.excluded.update({"automoderator", "[deleted]", self.reddit_account_lower})
        self.own_replies_map = own_replies_map or {}
        # Cache thread_id -> post_id lookups across a single scan so we don't
        # hit /api/v1/posts once per inbox entry (the same thread often
        # appears multiple times in a single page).
        self._post_id_cache = {}
        self.discovered = 0
        self.skipped_old = 0
        self.skipped_other = 0
        self.already_replied = 0
        self.unmatched = 0
        self.total_seen = 0

    def _post_id_for_context(self, context):
        m = THREAD_ID_RE.search(context or "")
        if not m:
            return None
        thread_id = m.group(1)
        if thread_id in self._post_id_cache:
            return self._post_id_cache[thread_id]
        # /api/v1/posts GET supports a platform filter but not LIKE on
        # thread_url. We fetch a window of recent reddit posts and match
        # locally on the thread_id substring, falling back to the lookup
        # endpoint with the same thread_id prefix.
        post_id = None
        try:
            resp = api_get(
                "/api/v1/posts",
                query={"platform": "reddit", "limit": 500},
            )
            posts = ((resp or {}).get("data") or {}).get("posts") or []
            for p in posts:
                tu = (p.get("thread_url") or "").lower()
                if f"/comments/{thread_id}/" in tu:
                    post_id = int(p.get("id"))
                    break
        except Exception:
            post_id = None
        self._post_id_cache[thread_id] = post_id
        return post_id

    def _insert(self, post_id, comment_id, author, content, comment_url, status, skip_reason=None):
        override = self.own_replies_map.get(comment_id)
        if override:
            from datetime import datetime, timezone
            ts = override.get("replied_at")
            replied_at = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            result = _insert_reply(
                self.db, post_id, "reddit", comment_id, author, content, comment_url,
                parent_reply_id=None, depth=1, status="replied", skip_reason=None,
                our_reply_id=override.get("our_reply_id"),
                our_reply_content=override.get("our_reply_content"),
                our_reply_url=override.get("our_reply_url"),
                replied_at=replied_at,
            )
            if result == "replied":
                self.already_replied += 1
            return
        result = _insert_reply(
            self.db, post_id, "reddit", comment_id, author, content, comment_url,
            parent_reply_id=None, depth=1, status=status, skip_reason=skip_reason,
        )
        if result == "pending":
            self.discovered += 1
        elif result == "skipped":
            self.skipped_old += 1

    def scan(self):
        print(f"Scanning inbox for u/{self.reddit_account}...")
        backfill_cutoff = time.time() - BACKFILL_HOURS * 3600
        after = None
        consecutive_known = 0
        for page in range(1, MAX_PAGES + 1):
            data = fetch_inbox(self.cookie_header, self.user_agent, after=after)
            children = data.get("children", [])
            print(f"  page {page}: {len(children)} items (after={after or 'start'})")
            if not children:
                break
            for c in children:
                self.total_seen += 1
                d = c.get("data", {})
                comment_id = (d.get("name") or "").removeprefix("t1_").removeprefix("t4_")
                if not comment_id:
                    continue
                author = d.get("author") or "[deleted]"
                if author.lower() in self.excluded:
                    self.skipped_other += 1
                    continue
                context = d.get("context") or ""
                post_id = self._post_id_for_context(context)
                if not post_id:
                    self.unmatched += 1
                    continue
                comment_url = "https://old.reddit.com" + context.split("?")[0]
                content = d.get("body") or ""
                created = float(d.get("created_utc") or 0)
                if created and created < backfill_cutoff:
                    pre = self.discovered + self.skipped_old
                    self._insert(post_id, comment_id, author, content, comment_url,
                                 status="skipped", skip_reason="backfill_old")
                    if (self.discovered + self.skipped_old) == pre:
                        consecutive_known += 1
                    else:
                        consecutive_known = 0
                else:
                    pre = self.discovered
                    self._insert(post_id, comment_id, author, content, comment_url,
                                 status="pending")
                    if self.discovered == pre:
                        consecutive_known += 1
                    else:
                        consecutive_known = 0
            # Always finish processing the current page before deciding whether
            # to fetch the next one. Bailing mid-page (the previous behavior)
            # could miss out-of-order items on the same page; the cost of
            # finishing the page is essentially zero (idempotent INSERTs only).
            # The 50-consecutive-known threshold now gates pagination only.
            if consecutive_known >= 50:
                print(f"  hit {consecutive_known} consecutive already-known items on page {page}, stopping pagination")
                return
            after = data.get("after")
            if not after:
                break
            if page < MAX_PAGES:
                time.sleep(PAGE_PAUSE_SECS)

    def finish(self):
        # All writes go through the HTTP API; nothing to commit/close locally.
        print(
            f"Inbox scan complete: seen={self.total_seen} "
            f"new_pending={self.discovered} backfill_skipped={self.skipped_old} "
            f"already_replied={self.already_replied} "
            f"excluded_author={self.skipped_other} unmatched_thread={self.unmatched}"
        )
        return {
            "discovered": self.discovered,
            "backfill_skipped": self.skipped_old,
            "already_replied": self.already_replied,
            "excluded": self.skipped_other,
            "unmatched": self.unmatched,
            "total_seen": self.total_seen,
        }


def run_engage(limit, timeout):
    print(f"\nFiring engage_reddit.py --platform reddit --limit {limit}...")
    proc = subprocess.run(
        ["python3", ENGAGE_SCRIPT, "--platform", "reddit", "--limit", str(limit), "--timeout", str(timeout)],
        cwd=os.path.dirname(ENGAGE_SCRIPT),
    )
    print(f"engage_reddit exit code: {proc.returncode}")
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description="Scan Reddit inbox for new replies, then engage")
    parser.add_argument("--reddit-account", default=None)
    parser.add_argument("--engage-limit", type=int, default=5,
                        help="Max replies to post per run (default: 5; 0 = skip engage)")
    parser.add_argument("--engage-timeout", type=int, default=600,
                        help="Total seconds for the engage subprocess (default: 600)")
    parser.add_argument("--no-engage", action="store_true",
                        help="Discovery only, don't fire engage_reddit.py")
    parser.add_argument("--no-jitter", action="store_true",
                        help="Skip the random startup jitter (use for manual runs)")
    args = parser.parse_args()

    config = load_config()
    # CLI override -> the ONE resolver (env -> reddit_account login truth ->
    # accounts.reddit.username).
    from account_resolver import resolve as _resolve_account
    reddit_account = args.reddit_account or _resolve_account("reddit") or ""
    if not reddit_account:
        print("ERROR: Reddit account not configured. Set it in config.json or pass --reddit-account")
        sys.exit(1)

    if not args.no_jitter:
        jitter = random.uniform(0, JITTER_MAX_SECS)
        print(f"Jitter: sleeping {jitter:.1f}s before scan")
        time.sleep(jitter)

    cookie_header = load_cookies()
    if not cookie_header:
        print(f"SESSION_INVALID: no cookie file at {COOKIES_PATH}. Run bootstrap_reddit_cookies.py.")
        sys.exit(0)

    user_agent = f"social-autoposter/1.0 (u/{reddit_account} inbox-scan)"
    excluded_authors = {a for a in config.get("exclusions", {}).get("authors", [])}
    own_replies_map = fetch_own_replies(reddit_account, cookie_header, user_agent)
    print(f"Own-replies map: {len(own_replies_map)} parent comment_ids we've already replied to")
    scanner = InboxScanner(reddit_account, user_agent, cookie_header,
                           excluded_authors=excluded_authors,
                           own_replies_map=own_replies_map)
    try:
        scanner.scan()
    except SessionInvalidError as e:
        print(f"SESSION_INVALID: {e}")
        scanner.finish()
        sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print(f"SESSION_INVALID: HTTP {e.code} on inbox endpoint. Refresh cookies via bootstrap_reddit_cookies.py.")
            scanner.finish()
            sys.exit(0)
        print(f"ERROR: HTTP {e.code} {e.reason}")
        scanner.finish()
        sys.exit(1)
    result = scanner.finish()

    if args.no_engage or args.engage_limit <= 0:
        print("Skipping engage step (per flags)")
        sys.exit(0)

    run_engage(args.engage_limit, args.engage_timeout)


if __name__ == "__main__":
    main()
