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


def browser_get_json(url, cdp_url=None, timeout_ms=25000):
    """Fetch a Reddit JSON URL through the logged-in harness Chrome.

    Returns (body_str_or_None, http_status_int). On any transport/connect
    failure returns (None, 0) so the caller can fall back to urllib.
    """
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
            # Close ONLY our ephemeral page; never the shared harness browser/
            # context. Calling browser.close() on a connect_over_cdp browser can
            # terminate the real Chrome (see reddit_browser.py warning), so we do
            # NOT close it. The sync_playwright() context exit disconnects the CDP
            # client cleanly without killing the remote browser.
            try:
                if page is not None:
                    page.close()
            except Exception:
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
