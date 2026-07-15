#!/usr/bin/env python3
"""Browser-backed Reddit JSON fetch (reddit-harness transport).

Why this exists: Reddit started returning HTTP 403 on *.json endpoints to
Python urllib/curl from residential IPs on 2026-05-28 (TLS-fingerprint +
no-cookies block). The exact same request issued from inside a logged-in real
Chrome page returns 200. So reddit_tools.py routes its discovery fetches through
here instead of urllib.

Transport: connect over CDP to the reddit-harness Chrome (REDDIT_CDP_URL,
default http://127.0.0.1:9557, profile ~/.claude/browser-profiles/reddit-harness),
open an ephemeral page, NAVIGATE it to the target URL's host root (so the page
origin matches the JSON endpoint), then issue a SAME-ORIGIN fetch() with
credentials. This is the path validated to return 200:
  - a bare top-level navigation straight to the .json URL still 403s (verified
    2026-05-29) — Reddit's bot wall keys on more than cookies for nav requests
  - a same-origin fetch() from a fully-loaded reddit.com page returns 200 with
    the logged-in session's cookies + referer + page fingerprint
  - navigating to the *matching* host first (www vs old) keeps the fetch
    same-origin, so no CORS block between www.reddit.com and old.reddit.com

Public API:
    browser_get_json(url, cdp_url=None, timeout_ms=25000) -> (body_str|None, status_int)
        body_str is the raw response text on HTTP 200, else None.
        status_int is the HTTP status (0 on transport failure).

CLI (for manual testing):
    python3 scripts/reddit_browser_fetch.py "https://www.reddit.com/search.json?q=test&limit=2"
"""

import os
import sys
import time
from urllib.parse import urlparse


def _default_cdp_url():
    return os.environ.get("REDDIT_CDP_URL", "http://127.0.0.1:9557").strip() \
        or "http://127.0.0.1:9557"


_POSTING_FLAG = os.path.join(
    os.path.expanduser(os.environ.get("S4L_STATE_DIR") or "~/.social-autoposter-mcp"),
    "reddit-posting-active.json",
)
_POSTING_FRESH_S = 120
_POSTING_YIELD_MAX_S = 300


def _yield_to_poster():
    """Posting owns the ONE shared harness tab (2026-07-14). Every reader
    fetch navigates the tab to the reddit host root, which is exactly what
    yanked the tab out from under mid-post drains (the tab_contention /
    false account_blocked_in_sub family). A fresh reddit-posting-active.json
    (heartbeated per row by post_reddit.py) means a poster is mid-drain:
    WAIT for it instead of navigating, bounded so a stale flag can never
    starve discovery/stats (they just run a few minutes later)."""
    deadline = time.time() + _POSTING_YIELD_MAX_S
    waited = False
    while time.time() < deadline:
        try:
            age = time.time() - os.path.getmtime(_POSTING_FLAG)
        except OSError:
            break  # no flag -> no poster
        if age >= _POSTING_FRESH_S:
            break  # stale flag -> dead poster; never blocks readers
        waited = True
        time.sleep(5)
    if waited:
        sys.stderr.write("[reddit_browser_fetch] yielded to active poster\n")


def browser_get_json(url, cdp_url=None, timeout_ms=25000):
    """Fetch a Reddit JSON URL through the logged-in harness Chrome.

    Returns (body_str_or_None, http_status_int). On any transport/connect
    failure returns (None, 0) so the caller can fall back to urllib.
    """
    _yield_to_poster()
    cdp_url = (cdp_url or _default_cdp_url())
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # playwright not importable -> signal transport fail
        sys.stderr.write(f"[reddit_browser_fetch] playwright import failed: {e}\n")
        return None, 0

    parsed = urlparse(url)
    host = parsed.netloc or "www.reddit.com"
    host_root = f"{parsed.scheme or 'https'}://{host}/"

    with sync_playwright() as p:
        browser = None
        page = None
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                sys.stderr.write("[reddit_browser_fetch] no CDP contexts on harness\n")
                return None, 0
            ctx = browser.contexts[0]
            # Reuse an existing tab instead of new_page() on every fetch. new_page()
            # steals OS focus each call (there are many discovery fetches per cycle,
            # so this churned the user's focus constantly); navigating a background
            # tab does not. Prefer a tab already on reddit.com; else pages[0]; else
            # create one. Mirrors reddit_browser / twitter_browser tab reuse. The
            # page is left OPEN for the next fetch (cleanup_harness_tabs trims to one
            # at cycle start).
            page = None
            for pg in ctx.pages:
                if "reddit.com" in (pg.url or "") and "login" not in (pg.url or ""):
                    page = pg
                    break
            if page is None and ctx.pages:
                page = ctx.pages[0]
            if page is None:
                page = ctx.new_page()
            # Load the matching host root so the subsequent fetch() is same-origin
            # (no CORS between www/old) and carries the logged-in session.
            try:
                page.goto(host_root, wait_until="load", timeout=timeout_ms)
            except Exception:
                pass  # partial load is fine; we just need an active reddit origin
            # Same-origin fetch with a couple retries — reddit.com sometimes does a
            # client redirect on first load that destroys the execution context.
            js = (
                "async (u) => {"
                "  const r = await fetch(u, {credentials:'include',"
                "                            headers:{'Accept':'application/json'}});"
                "  const t = await r.text();"
                "  return {status: r.status, body: t};"
                "}"
            )
            last_err = None
            for attempt in range(3):
                try:
                    res = page.evaluate(js, url)
                    status = int(res.get("status", 0))
                    body = res.get("body") or ""
                    if status != 200:
                        return None, status
                    return body, status
                except Exception as e:
                    last_err = e
                    time.sleep(2.0)  # let a redirect settle, then retry
            sys.stderr.write(f"[reddit_browser_fetch] evaluate failed after retries: {last_err}\n")
            return None, 0
        except Exception as e:
            sys.stderr.write(f"[reddit_browser_fetch] error: {e}\n")
            return None, 0
        finally:
            # Do NOT close the page: it is a REUSED tab, and closing it forces the
            # next fetch to new_page() which steals OS focus. Leaving it open lets
            # the next fetch reuse it (cleanup_harness_tabs trims to one at cycle
            # start). Also never close the connect_over_cdp browser/context: that can
            # terminate the real harness Chrome (see reddit_browser.py warning). The
            # sync_playwright() context exit disconnects the CDP client cleanly
            # without killing the remote browser.
            pass


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: reddit_browser_fetch.py <reddit-json-url>\n")
        return 2
    body, status = browser_get_json(argv[1])
    sys.stderr.write(f"[reddit_browser_fetch] status={status} bytes={len(body) if body else 0}\n")
    if body:
        sys.stdout.write(body)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
