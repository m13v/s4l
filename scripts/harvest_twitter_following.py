#!/usr/bin/env python3
"""harvest_twitter_following.py — cache the list of accounts WE follow on X.

The Twitter reply pipeline (score_twitter_candidates.py) drops candidate threads
whose author is someone we already follow. fxtwitter can't supply that edge — it's
an unauthenticated public API with no concept of "us" — so the follow relationship
has to be read from our own logged-in session. This script scrapes
`x.com/<handle>/following` via the harness Chrome (CDP, port 9555, same browser the
cycle uses) and uploads the set to /api/v1/followed-accounts.

Read-only: ONE navigation + DOM reads + scrolls. No clicks, no posting, no
/voyager. Runs under the shared "twitter-browser" lock (held by the shell wrapper
skill/refresh-twitter-following.sh) so it never races a live cycle.

Completeness guard: we only upload when the scroll reached the end of the list
(the deduped set stopped growing for STABLE_PASSES passes). A partial scrape is
discarded, never uploaded — otherwise the un-scrolled tail would wrongly age out
of the server's freshness window.

Usage:
    python3 scripts/harvest_twitter_following.py            # scrape + upload
    python3 scripts/harvest_twitter_following.py --dry-run  # scrape + print, no upload
    python3 scripts/harvest_twitter_following.py --out /tmp/following.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CDP_URL = os.environ.get("TWITTER_CDP_URL", "http://127.0.0.1:9555").strip()
PLATFORM = "twitter"

# Scroll/scrape tuning (env-overridable for slow boxes / very large lists).
STABLE_PASSES = int(os.environ.get("FOLLOW_HARVEST_STABLE_PASSES", "5"))
MAX_PASSES = int(os.environ.get("FOLLOW_HARVEST_MAX_PASSES", "800"))
PAUSE_MS = int(os.environ.get("FOLLOW_HARVEST_PAUSE_MS", "900"))
UPLOAD_CHUNK = int(os.environ.get("FOLLOW_HARVEST_UPLOAD_CHUNK", "1000"))

# Each row on the Following tab is a [data-testid="UserCell"]. The profile link
# href is exactly `/<screen_name>`; grab the first anchor matching that shape
# (X handles are 1-15 chars of [A-Za-z0-9_]) that isn't a reserved app route.
SCRAPE_JS = r"""
(() => {
  const RESERVED = new Set(['home','explore','notifications','messages','i',
    'settings','search','compose','hashtag','intent','login','signup','tos',
    'privacy','about']);
  const cells = Array.from(document.querySelectorAll('[data-testid="UserCell"]'));
  const out = [];
  for (const c of cells) {
    let handle = null;
    for (const a of c.querySelectorAll('a[href^="/"]')) {
      const m = (a.getAttribute('href') || '').match(/^\/([A-Za-z0-9_]{1,15})$/);
      if (m && !RESERVED.has(m[1].toLowerCase())) { handle = m[1]; break; }
    }
    if (!handle) continue;
    let name = null;
    const un = c.querySelector('[data-testid="User-Name"]');
    if (un) {
      // User-Name mashes "Display Name@handle…"; the display name is the text
      // before the first '@'.
      name = ((un.textContent || '').split('@')[0]).trim().slice(0, 120) || null;
    }
    out.push({ screen_name: handle, name });
  }
  return JSON.stringify(out);
})()
"""


def _resolve_handle() -> str:
    try:
        import account_resolver
        h = account_resolver.resolve("twitter")
        if h:
            return h.lstrip("@").strip().lower()
    except Exception as e:
        print(f"[harvest] account_resolver failed ({e}); falling back to m13v_",
              file=sys.stderr)
    return "m13v_"


def _looks_logged_out(url: str) -> bool:
    u = (url or "").lower()
    return ("/login" in u) or ("i/flow/login" in u) or ("/account/access" in u)


def scrape_following(handle: str) -> tuple[dict, bool]:
    """Return (handle->name dict, complete). complete=True means the scroll
    reached the end (set stopped growing) rather than hitting the pass cap."""
    from playwright.sync_api import sync_playwright

    seen: dict[str, str] = {}
    complete = False
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("no browser context on harness Chrome — is it logged in?")
        context = contexts[0]
        # Reuse an existing tab (tab hygiene); fall back to a fresh page.
        page = context.pages[0] if context.pages else context.new_page()

        url = f"https://x.com/{handle}/following"
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        if _looks_logged_out(page.url):
            raise RuntimeError(f"session looks logged out (url={page.url})")

        # Wait for at least one row to render before scrolling.
        try:
            page.wait_for_selector('[data-testid="UserCell"]', timeout=20000)
        except Exception:
            # No cells at all — empty list, protected, or a block page. Treat as
            # incomplete so we never upload an empty/partial set.
            print(f"[harvest] no UserCell rendered for @{handle} (url={page.url})",
                  file=sys.stderr)
            return seen, False

        last = 0
        stable = 0
        for i in range(MAX_PASSES):
            try:
                raw = page.evaluate(SCRAPE_JS)
                rows = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except Exception as e:
                print(f"[harvest] evaluate failed on pass {i} ({e})", file=sys.stderr)
                rows = []
            for r in rows:
                sn = (r.get("screen_name") or "").strip().lower()
                if not sn or sn == handle:  # never list ourselves
                    continue
                if sn not in seen:
                    seen[sn] = r.get("name") or ""

            if len(seen) == last:
                stable += 1
                if stable >= STABLE_PASSES:
                    complete = True
                    break
            else:
                stable = 0
                last = len(seen)

            page.evaluate(
                "window.scrollBy(0, Math.round(document.documentElement.clientHeight * 0.85));"
            )
            page.wait_for_timeout(PAUSE_MS)

        # Disconnect the CDP client without closing the shared Chrome/tab.
        try:
            browser.close()
        except Exception:
            pass

    print(
        f"[harvest] @{handle}: collected {len(seen)} followed handles "
        f"(complete={complete}, passes_stable={stable}/{STABLE_PASSES})",
        file=sys.stderr,
    )
    return seen, complete


def upload(handle: str, seen: dict) -> int:
    from http_api import api_post

    accounts = [{"handle": h, "name": n} for h, n in seen.items()]
    posted = 0
    for i in range(0, len(accounts), UPLOAD_CHUNK):
        chunk = accounts[i:i + UPLOAD_CHUNK]
        api_post(
            "/api/v1/followed-accounts",
            {
                "platform": PLATFORM,
                "our_account": handle,
                "accounts": chunk,
                "complete": True,
            },
        )
        posted += len(chunk)
    return posted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape and report but do not upload.")
    parser.add_argument("--out", help="Also write the scraped set to this JSON path.")
    parser.add_argument("--handle", help="Override the resolved posting handle.")
    args = parser.parse_args()

    handle = (args.handle or _resolve_handle()).lstrip("@").strip().lower()
    print(f"[harvest] resolving following list for @{handle} via {CDP_URL}",
          file=sys.stderr)

    try:
        seen, complete = scrape_following(handle)
    except Exception as e:
        print(f"[harvest] FAILED: {e}", file=sys.stderr)
        return 1

    if args.out:
        try:
            with open(args.out, "w") as fh:
                json.dump({"handle": handle, "complete": complete,
                           "accounts": seen}, fh, indent=2)
            print(f"[harvest] wrote scrape to {args.out}", file=sys.stderr)
        except OSError as e:
            print(f"[harvest] could not write {args.out}: {e}", file=sys.stderr)

    if not seen:
        print("[harvest] scraped 0 handles; nothing to upload.", file=sys.stderr)
        return 2
    if not complete:
        print(
            f"[harvest] scrape INCOMPLETE (hit {MAX_PASSES}-pass cap at "
            f"{len(seen)} handles); NOT uploading, to avoid aging out the "
            f"un-scrolled tail. Re-run will retry.",
            file=sys.stderr,
        )
        return 3
    if args.dry_run:
        print(f"[harvest] dry-run: would upload {len(seen)} handles for @{handle}.")
        return 0

    posted = upload(handle, seen)
    print(f"[harvest] uploaded {posted} followed handles for @{handle}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
