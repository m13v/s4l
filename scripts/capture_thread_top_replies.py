#!/usr/bin/env python3
"""capture_thread_top_replies.py - snapshot top N existing replies on threads
where we (recently) posted a comment, so we can benchmark our reply's
engagement curve against the human top-reply curve over time.

This script is intentionally DECOUPLED from the in-flow posting path
(`scripts/twitter_post_plan.py` is locked; we don't edit it). Instead, it
polls `posts` for fresh twitter comment-rows that don't yet have a top-reply
snapshot, attaches to the running twitter-harness Chrome via CDP, navigates
to each thread, scrapes the top 3 replies (Twitter default-sort = "Most
relevant" ≈ likes-weighted), and INSERTs into `thread_top_replies` while
stamping `posts.top_replies_captured_at` so the same row isn't reprocessed.

Tradeoff: the snapshot lags posted_at by ~1-2 min (whatever the launchd
cadence is). For threads older than ~10 min at our-post time, the top-reply
set is essentially stationary, so the lag is acceptable. For very fresh
threads (our post within minutes of the OP) the top-reply set is more
volatile, so `likes_at_capture` is "near-post-time" not "exact-post-time".

Driven by launchd `com.m13v.social-capture-twitter-top-replies` (~2 min).

Usage:
    python3 scripts/capture_thread_top_replies.py            # default cadence run
    python3 scripts/capture_thread_top_replies.py --post-id N  # capture one specific post
    python3 scripts/capture_thread_top_replies.py --window-hours 2 --limit 10
    python3 scripts/capture_thread_top_replies.py --dry-run   # scrape but don't write
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure scripts/ is on sys.path so `import db` works regardless of CWD.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from db import get_conn  # noqa: E402


# ---------------------------------------------------------------------------
# Twitter browser lock (compat with scripts/twitter_browser.py:LOCK_FILE).
# Defer if another active python:PID holder is mid-run — we don't want to
# stomp on a concurrent posting cycle inside the same harness Chrome.
# ---------------------------------------------------------------------------

LOCK_FILE = os.path.expanduser("~/.claude/twitter-browser-lock.json")
LOCK_EXPIRY_SEC = 600  # treat anything older than 10 min as stale
_LOCK_SESSION_ID = f"python-capture:{os.getpid()}"
_LOCK_HELD = False


def _pid_alive(holder: str) -> bool:
    """Holder format is `python[-tag]:<pid>`; check that PID is still alive."""
    try:
        pid_part = holder.split(":")[-1]
        pid = int(pid_part)
    except (ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_twitter_lock(timeout_sec: int = 0) -> bool:
    """Try to acquire the twitter browser lock.

    Returns True on success, False if a live holder still owns it. We do NOT
    wait by default — better to skip a cron tick than to queue up behind a
    long-running cycle and pile work.
    """
    global _LOCK_HELD
    deadline = time.time() + timeout_sec
    while True:
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE) as f:
                    lock = json.load(f)
                holder = lock.get("session_id", "") or ""
                age = time.time() - lock.get("timestamp", 0)
                # Stale by age or holder dead → take it.
                if age >= LOCK_EXPIRY_SEC:
                    break
                # python:PID holder with dead PID → stale, take it.
                if holder.startswith("python") and not _pid_alive(holder):
                    break
                # Live holder. Wait until deadline (default = 0 = don't wait).
                if time.time() >= deadline:
                    return False
                time.sleep(0.5)
                continue
            except (json.JSONDecodeError, OSError):
                # Corrupt lockfile — assume safe to take.
                break
        break
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        with open(LOCK_FILE, "w") as f:
            json.dump({"session_id": _LOCK_SESSION_ID,
                       "timestamp": int(time.time())}, f)
        _LOCK_HELD = True
        return True
    except OSError:
        return False


def release_twitter_lock() -> None:
    global _LOCK_HELD
    if not _LOCK_HELD:
        return
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                lock = json.load(f)
            if lock.get("session_id") == _LOCK_SESSION_ID:
                os.remove(LOCK_FILE)
    except (OSError, json.JSONDecodeError):
        pass
    _LOCK_HELD = False


atexit.register(release_twitter_lock)


# ---------------------------------------------------------------------------
# CDP discovery + Playwright attach (mirrors the pattern from
# scripts/twitter_browser.py:find_twitter_cdp_port / get_browser_and_page,
# kept inline so we don't import a locked module's side effects).
# ---------------------------------------------------------------------------

def find_twitter_cdp_port() -> int | None:
    """Scan Chrome processes for --remote-debugging-port; return the first
    one serving an x.com / twitter.com tab.
    """
    try:
        ps_out = subprocess.check_output(["ps", "aux"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    import urllib.request
    ports = set()
    for line in ps_out.splitlines():
        if "chromium" not in line.lower() and "chrome" not in line.lower():
            continue
        m = re.search(r"remote-debugging-port=(\d+)", line)
        if m:
            ports.add(int(m.group(1)))
    best_port = None
    for port in sorted(ports):
        try:
            resp = urllib.request.urlopen(f"http://localhost:{port}/json", timeout=2)
            pages = json.loads(resp.read())
            twitter_pages = [
                p for p in pages
                if "x.com" in p.get("url", "") or "twitter.com" in p.get("url", "")
            ]
            if not twitter_pages:
                continue
            # Prefer ports with logged-in pages (home, status, notifications)
            logged_in = any(
                ("home" in p.get("url", "") or "status" in p.get("url", "") or
                 "notifications" in p.get("url", "") or "chat" in p.get("url", ""))
                and "login" not in p.get("url", "")
                for p in twitter_pages
            )
            if logged_in:
                return port
            if best_port is None:
                best_port = port
        except Exception:
            continue
    return best_port


# ---------------------------------------------------------------------------
# Twitter count-parsing
# ---------------------------------------------------------------------------

_COUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([KMB]?)", re.IGNORECASE)


def parse_twitter_count(s: str | None) -> int | None:
    """Parse '1.2K', '342', '5,123', '1M' into an integer.

    Twitter aria-labels look like '143 likes' or '1.2K replies'; we strip
    everything but the numeric prefix and the optional K/M/B suffix.
    """
    if not s:
        return None
    s = s.strip().replace(",", "")
    m = _COUNT_RE.search(s)
    if not m:
        return None
    num = float(m.group(1))
    suf = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suf]
    return int(num * mult)


# ---------------------------------------------------------------------------
# Page-side scraper. Runs inside the harness Chrome via page.evaluate().
# Returns a list of {rank, reply_url, reply_tweet_id, reply_author,
# reply_author_handle, reply_content, likes, replies, retweets, views}.
# We take the first 3 article[data-testid="tweet"] elements AFTER the parent
# tweet (the one whose status link matches the thread URL).
# ---------------------------------------------------------------------------

SCRAPE_JS = r"""
() => {
  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  if (articles.length === 0) return { ok: false, reason: "no_articles" };

  const threadId = (location.pathname.match(/\/status\/(\d+)/) || [])[1];
  if (!threadId) return { ok: false, reason: "no_thread_id" };

  // Find the parent tweet: first article that contains a time-anchored link
  // to `/status/<threadId>`. If we can't find it, fall back to article[0].
  let parentIdx = -1;
  for (let i = 0; i < articles.length; i++) {
    const timeLinks = articles[i].querySelectorAll('a[href*="/status/"]');
    for (const a of timeLinks) {
      const href = a.getAttribute('href') || '';
      if (href.includes('/status/' + threadId) && a.querySelector('time')) {
        parentIdx = i;
        break;
      }
    }
    if (parentIdx >= 0) break;
  }
  if (parentIdx < 0) parentIdx = 0;

  // Take all articles after the parent. Caller filters + sorts in Python.
  // Caps at 30 to keep evaluate() payload tight.
  const replies = articles.slice(parentIdx + 1, parentIdx + 1 + 30);
  if (replies.length === 0) return { ok: true, replies: [] };

  // Twitter uses aria-label on the action buttons in the form:
  //   "143 replies, 12 reposts, 1.2K likes, 25 bookmarks, 5,432 views"
  // The full label hangs off the group wrapper [role="group"][aria-label].
  // Per-button aria-labels are more reliable for newer DOMs.
  const parseCountLabel = (label) => {
    if (!label) return null;
    const m = label.match(/([\d.,]+)\s*([KMB]?)/i);
    if (!m) return 0;
    const num = parseFloat(m[1].replace(/,/g, ''));
    const suf = (m[2] || '').toUpperCase();
    const mult = { "": 1, K: 1e3, M: 1e6, B: 1e9 }[suf];
    return Math.round(num * mult);
  };

  const out = [];
  for (let i = 0; i < replies.length; i++) {
    const art = replies[i];

    // Permalink: prefer the link wrapping the <time> element.
    let permalink = null;
    const timeLinks = art.querySelectorAll('a[href*="/status/"]');
    for (const a of timeLinks) {
      if (a.querySelector('time')) { permalink = a.href; break; }
    }
    if (!permalink && timeLinks[0]) permalink = timeLinks[0].href;

    const tweetIdMatch = permalink ? permalink.match(/\/status\/(\d+)/) : null;
    const replyId = tweetIdMatch ? tweetIdMatch[1] : null;

    // Author handle = the path segment before /status/.
    let handle = null;
    if (permalink) {
      const u = new URL(permalink);
      const m = u.pathname.match(/^\/([^/]+)\/status\//);
      if (m) handle = m[1];
    }

    // Display name: first User-Name span text. data-testid="User-Name" wraps
    // both display + handle; take the first span with a text node.
    let displayName = null;
    const nameEl = art.querySelector('[data-testid="User-Name"]');
    if (nameEl) {
      const span = nameEl.querySelector('span');
      if (span) displayName = (span.innerText || '').trim() || null;
    }

    // Tweet text
    const textEl = art.querySelector('[data-testid="tweetText"]');
    const text = textEl ? (textEl.innerText || '').trim() : null;

    // Engagement counts via per-action aria-label.
    const btnLabel = (selector) => {
      const el = art.querySelector(selector);
      if (!el) return null;
      return el.getAttribute('aria-label') || null;
    };
    const likes = parseCountLabel(btnLabel('[data-testid="like"]'));
    const replyN = parseCountLabel(btnLabel('[data-testid="reply"]'));
    const retweets = parseCountLabel(btnLabel('[data-testid="retweet"]'));

    // Views: there's an anchor to /analytics with aria-label like "5,432 views".
    let views = null;
    const viewLinks = art.querySelectorAll('a[href$="/analytics"], a[aria-label*="View"]');
    for (const a of viewLinks) {
      const v = parseCountLabel(a.getAttribute('aria-label'));
      if (v != null) { views = v; break; }
    }
    // Fallback: scrape the group wrapper.
    if (views == null) {
      const group = art.querySelector('[role="group"][aria-label]');
      if (group) {
        const lbl = group.getAttribute('aria-label') || '';
        const vm = lbl.match(/([\d.,]+)\s*([KMB]?)\s*views?/i);
        if (vm) {
          const num = parseFloat(vm[1].replace(/,/g, ''));
          const suf = (vm[2] || '').toUpperCase();
          const mult = { "": 1, K: 1e3, M: 1e6, B: 1e9 }[suf];
          views = Math.round(num * mult);
        }
      }
    }

    out.push({
      // raw_index = order Twitter showed them; we'll re-rank by likes in Python.
      raw_index: i,
      reply_url: permalink,
      reply_tweet_id: replyId,
      reply_author: displayName,
      reply_author_handle: handle,
      reply_content: text,
      likes, replies: replyN, retweets, views,
    });
  }
  return { ok: true, replies: out };
}
"""


def scrape_thread(page, thread_url: str, our_handle: str | None,
                  thread_author_handle: str | None = None,
                  top_n: int = 3, log_prefix: str = "") -> dict:
    """Navigate to thread_url, scroll to lazy-load replies, then return the
    top N human replies sorted by like count.

    Filters out: our own posting handle (we don't compete with ourselves),
    the thread author (their self-replies and quote-continuations don't
    represent the human "best comment" benchmark we're measuring against),
    pinned-context tweets (replied-to context shown above the parent), and
    replies with no permalink (the embedded "Show more replies" rows).
    """
    # Normalize the URL so we hit x.com (the harness usually has x.com tabs).
    url = thread_url.replace("twitter.com", "x.com")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        return {"ok": False, "reason": f"goto_failed: {e}"}

    # Wait for at least 2 articles to appear (parent + at least one reply).
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            count = page.evaluate(
                "() => document.querySelectorAll('article[data-testid=\"tweet\"]').length"
            )
            if (count or 0) >= 2:
                break
        except Exception:
            pass
        time.sleep(0.5)

    # Scroll a few times to lazy-load more replies. Twitter only renders ~3-5
    # in the initial viewport; scrolling pulls in the rest of the thread.
    for _ in range(4):
        try:
            page.evaluate("() => window.scrollBy(0, window.innerHeight * 1.2)")
        except Exception:
            break
        time.sleep(0.8)

    # Scroll back up so the JS evaluator sees the parent tweet first (the
    # parent-index lookup walks from the top of the article list).
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    time.sleep(0.8)

    # Give engagement counts a beat to hydrate (they render lazily).
    time.sleep(1.2)

    try:
        result = page.evaluate(SCRAPE_JS)
    except Exception as e:
        return {"ok": False, "reason": f"evaluate_failed: {e}"}

    if not isinstance(result, dict):
        return {"ok": False, "reason": "bad_result_shape"}

    if not result.get("ok"):
        return result

    raw_replies = result.get("replies") or []

    # ---- Filter and sort -------------------------------------------------
    our_handle_lc = (our_handle or "").lower().lstrip("@")
    op_handle_lc = (thread_author_handle or "").lower().lstrip("@")
    filtered = []
    seen_urls = set()
    for r in raw_replies:
        url_r = r.get("reply_url")
        if not url_r:
            continue
        # De-dup by permalink (same reply can appear twice if the DOM mounted
        # then re-mounted during our scroll).
        if url_r in seen_urls:
            continue
        seen_urls.add(url_r)
        handle = (r.get("reply_author_handle") or "").lower().lstrip("@")
        if our_handle_lc and handle == our_handle_lc:
            continue  # skip our own reply
        if op_handle_lc and handle == op_handle_lc:
            continue  # skip OP self-replies / quote-continuations
        filtered.append(r)

    # Sort by likes desc, then views desc as tiebreaker. None coerced to -1
    # so missing-stats replies sink to the bottom but don't crash compare.
    def sort_key(r):
        return (-(r.get("likes") or 0), -(r.get("views") or 0), r.get("raw_index", 999))
    filtered.sort(key=sort_key)

    top = filtered[:top_n]
    # Re-rank 1..N based on sorted order (this is rank_at_capture in the DB).
    for i, r in enumerate(top, start=1):
        r["rank"] = i

    if log_prefix:
        print(f"{log_prefix} scraped {len(raw_replies)} replies, filtered → "
              f"{len(filtered)} non-self, kept top {len(top)}", flush=True)
    return {"ok": True, "replies": top, "raw_count": len(raw_replies),
            "filtered_count": len(filtered)}


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def fetch_pending_posts(db, window_hours: int, limit: int, post_id: int | None):
    if post_id is not None:
        cur = db.execute(
            "SELECT id, thread_url, our_url, posted_at, thread_author_handle "
            "FROM posts WHERE id = %s",
            [post_id],
        )
        return [dict(r) for r in cur.fetchall()]
    cur = db.execute(
        "SELECT id, thread_url, our_url, posted_at, thread_author_handle "
        "FROM posts "
        "WHERE platform = 'twitter' "
        "  AND top_replies_captured_at IS NULL "
        "  AND posted_at > NOW() - INTERVAL '%s hours' "
        "  AND thread_url IS NOT NULL "
        "  AND thread_url <> our_url "  # only comment-rows, not own posts
        "ORDER BY posted_at DESC "
        "LIMIT %s",
        [window_hours, limit],
    )
    return [dict(r) for r in cur.fetchall()]


def insert_top_replies(db, post_id: int, thread_url: str, replies: list[dict]) -> int:
    """INSERT one row per scraped reply, then stamp posts.top_replies_captured_at.

    Wrapped in a single transaction so we either capture all 3 or none.
    UNIQUE constraint on (post_id, rank_at_capture) makes this idempotent on retry.
    """
    inserted = 0
    for r in replies:
        if not r.get("reply_url"):
            continue
        db.execute(
            "INSERT INTO thread_top_replies "
            "  (post_id, platform, thread_url, rank_at_capture, reply_url, "
            "   reply_tweet_id, reply_author, reply_author_handle, reply_content, "
            "   likes_at_capture, replies_at_capture, retweets_at_capture, views_at_capture, "
            "   likes, replies, retweets, views, engagement_updated_at) "
            "VALUES (%s, 'twitter', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()) "
            "ON CONFLICT (post_id, rank_at_capture) DO NOTHING",
            [
                post_id, thread_url, r["rank"], r["reply_url"],
                r.get("reply_tweet_id"), r.get("reply_author"),
                r.get("reply_author_handle"), r.get("reply_content"),
                r.get("likes"), r.get("replies"), r.get("retweets"), r.get("views"),
                r.get("likes"), r.get("replies"), r.get("retweets"), r.get("views"),
            ],
        )
        inserted += 1
    db.execute(
        "UPDATE posts SET top_replies_captured_at = NOW() WHERE id = %s",
        [post_id],
    )
    db.commit()
    return inserted


def mark_captured_empty(db, post_id: int) -> None:
    """Stamp capture timestamp even when there are zero replies on the
    thread (so we don't keep re-trying every cron tick).
    """
    db.execute(
        "UPDATE posts SET top_replies_captured_at = NOW() WHERE id = %s",
        [post_id],
    )
    db.commit()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-hours", type=int, default=2,
                    help="Look back this many hours for uncaptured twitter posts")
    ap.add_argument("--limit", type=int, default=5,
                    help="Max posts to process per run (keeps single-tick cheap)")
    ap.add_argument("--post-id", type=int, default=None,
                    help="Capture a single specific post id (ignores window/limit)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Scrape but don't write to DB")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    log = (lambda *a, **kw: None) if args.quiet else (lambda *a, **kw: print(*a, **kw, flush=True))

    db = get_conn()
    pending = fetch_pending_posts(
        db, window_hours=args.window_hours, limit=args.limit, post_id=args.post_id,
    )
    if not pending:
        log("[capture-top-replies] nothing to do (no uncaptured twitter posts in window)")
        return 0

    log(f"[capture-top-replies] {len(pending)} post(s) to process")

    # Acquire the twitter browser lock. We use our own page (not the cycle's
    # foreground tab), but the lock is the established "don't run concurrent
    # cycles in this harness" contract — respect it. If held by an active
    # holder, defer to the next cron tick.
    if not acquire_twitter_lock(timeout_sec=0):
        log("[capture-top-replies] twitter browser lock held by another cycle; deferring")
        return 0

    cdp_port = find_twitter_cdp_port()
    if not cdp_port:
        log("[capture-top-replies] no twitter-harness Chrome reachable; skipping run")
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("[capture-top-replies] playwright not installed; pip3 install playwright")
        return 3

    summary = {"scraped": 0, "replies_inserted": 0, "errors": 0, "empty": 0}

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
        except Exception as e:
            log(f"[capture-top-replies] CDP attach failed (port {cdp_port}): {e}")
            return 4

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        # Always create our OWN page. The harness may have a foreground tab
        # mid-post; we don't touch it. New tab gets closed before we return.
        page = ctx.new_page()

        for post in pending:
            pid = post["id"]
            thread_url = post["thread_url"]
            prefix = f"[capture-top-replies post={pid}]"

            # Extract our posting handle from our_url so we can skip our own reply.
            our_url = post.get("our_url") or ""
            our_handle_m = re.search(r"(?:x\.com|twitter\.com)/([^/]+)/status",
                                     our_url)
            our_handle = our_handle_m.group(1) if our_handle_m else None

            try:
                result = scrape_thread(
                    page, thread_url,
                    our_handle=our_handle,
                    thread_author_handle=post.get("thread_author_handle"),
                    log_prefix=prefix if not args.quiet else "",
                )
            except Exception as e:
                log(f"{prefix} scrape exception: {e}")
                summary["errors"] += 1
                continue

            if not result.get("ok"):
                log(f"{prefix} scrape failed: {result.get('reason')}")
                summary["errors"] += 1
                continue

            replies = result.get("replies") or []
            summary["scraped"] += 1

            if not replies:
                # Thread has 0 visible non-self replies. Could be a genuinely
                # empty thread, OR our reply is the only one + lazy load
                # didn't fire. Stamp captured_at so we don't loop forever,
                # unless this is a dry run.
                raw_count = result.get("raw_count", 0)
                if not args.dry_run:
                    mark_captured_empty(db, pid)
                    stamp_msg = "stamped captured_at"
                else:
                    stamp_msg = "would stamp captured_at (dry-run)"
                summary["empty"] += 1
                log(f"{prefix} 0 top non-self replies (raw scraped={raw_count}); {stamp_msg}")
                continue

            if args.dry_run:
                log(f"{prefix} DRY RUN — would insert {len(replies)} replies:")
                for r in replies:
                    log(f"  rank={r['rank']} @{r.get('reply_author_handle')} "
                        f"likes={r.get('likes')} views={r.get('views')} "
                        f"text={(r.get('reply_content') or '')[:80]!r}")
                continue

            try:
                inserted = insert_top_replies(db, pid, thread_url, replies)
            except Exception as e:
                log(f"{prefix} insert failed: {e}")
                summary["errors"] += 1
                continue

            summary["replies_inserted"] += inserted
            log(f"{prefix} inserted {inserted} top replies")

            # tiny pacing so we don't hammer Twitter on a multi-post batch
            time.sleep(2.0)

        # Close our scratch tab; leave the harness Chrome itself alone.
        try:
            page.close()
        except Exception:
            pass

    log(f"[capture-top-replies] done: {json.dumps(summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
