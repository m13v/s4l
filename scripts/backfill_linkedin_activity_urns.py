#!/usr/bin/env python3
"""One-off backfill: recover commentUrns for legacy LinkedIn posts rows.

Context (2026-05-11): linkedin_api.py:comment_on_post was patched today to
embed `?commentUrn=<urn>` in `posts.our_url` so the unified LinkedIn stats
pipeline can identify which comment is OURS on the activity feed. Posts
written BEFORE that patch stored `our_url = thread_url` (the parent post
URL only), so the stats pipeline reads the parent post's reactions /
comments instead of ours. About 1,022 legacy rows are affected.

This script recovers the commentUrn for as many of them as possible by:

  1. One navigation to /in/me/recent-activity/comments/ (the activity
     tab; LinkedIn's own UI for "comments I made"). This is the EXACT
     navigation the stats scraper uses, just with deeper scroll. No
     per-permalink hops (banned), no Voyager API (banned).
  2. Deep in-page scroll + harvest inside ONE page.evaluate(). For each
     <article data-urn^="urn:li:comment:..."> on the page, capture:
       - comment_id from URN
       - parent_kind, parent_id from URN
       - comment_text (the actual visible text we wrote)
  3. Match each scraped item against the DB. For each posts row where
       platform='linkedin' AND our_url ILIKE '%/feed/update/urn:li:%/'
       AND our_url NOT ILIKE '%commentUrn%'
       AND first 60 chars of our_content == first 60 chars of scraped text
     ...and the match is unique on both sides, update posts.our_url to
     include the recovered commentUrn.

LinkedIn-safety carve-out (matches existing stats scraper conventions):
  - Headed Chromium only. Inherits the linkedin-agent's persistent profile.
  - ONE page.goto. ONE page.evaluate. No clicks. No "Show more" buttons.
  - Read-only DOM walk. Treat session/checkpoint redirects as STOP.
  - This script is invoked manually, NOT by launchd.

Usage:
  # Phase 1 (scrape only, no DB changes):
  SOCIAL_AUTOPOSTER_LINKEDIN_BACKFILL=1 \\
  /usr/bin/python3 scripts/backfill_linkedin_activity_urns.py \\
      --out /tmp/li_backfill_feed.json --max-scrolls 80 --scrape

  # Phase 2 (preview match):
  /opt/homebrew/bin/python3 scripts/backfill_linkedin_activity_urns.py \\
      --in /tmp/li_backfill_feed.json --match --dry-run

  # Phase 3 (apply):
  /opt/homebrew/bin/python3 scripts/backfill_linkedin_activity_urns.py \\
      --in /tmp/li_backfill_feed.json --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


COMMENTS_URL = "https://www.linkedin.com/in/me/recent-activity/comments/"
SCROLL_PAUSE_MIN_MS = 3000
SCROLL_PAUSE_MAX_MS = 6000
SCROLL_DY_MIN = 600
SCROLL_DY_MAX = 1100
HARVEST_SETTLE_MS = 3000
# How many consecutive stagnant ticks (no new comments AND no scroll-height
# growth) before we give up. Increased from 5 to 20 on 2026-05-11 so the
# backfill harvest tolerates LinkedIn's lazy-loader's occasionally long
# pauses; with 5, we were stopping ~22 comments in.
STAGNANT_TOLERANCE = 20


# JS to extract per-article: URN + visible comment text. Mirrors the
# stats scraper's deep-scroll + harvest-into-Map shape so we don't miss
# comments that get virtualized out of view between scrolls.
HARVEST_JS = r"""
(opts) => new Promise(resolve => {
  const acc = new Map();
  const ticksLog = [];

  function harvest() {
    let added = 0;
    document.querySelectorAll('article').forEach(art => {
      const urnEl = art.querySelector(
        '[data-urn^="urn:li:comment:"], [data-id^="urn:li:comment:"]'
      );
      if (!urnEl) return;
      const urn = urnEl.getAttribute('data-urn')
                || urnEl.getAttribute('data-id') || '';
      const m = urn.match(/^urn:li:comment:\((?:urn:li:)?(\w+):(\d+),(\d+)\)$/);
      if (!m) return;
      const parent_kind = m[1], parent_id = m[2], comment_id = m[3];

      // Visible comment text: prefer the comments-thread-entity div's
      // text content (excluding nested replies). Fall back to article
      // text minus engagement leaves.
      let text = '';
      const threadEntity = art.querySelector('.comments-thread-entity');
      if (threadEntity) {
        text = (threadEntity.innerText || '').trim();
      } else {
        text = (art.innerText || '').trim();
      }
      // Strip the engagement bar suffix that lives at the end of the
      // text node (Like|N|Reply|...). We only want the comment body.
      // The body ends right before the first occurrence of \nLike\n.
      const stopIdx = text.indexOf('\nLike\n');
      if (stopIdx > 0) text = text.slice(0, stopIdx).trim();

      // Strip the leading meta line ("Matthew Diakonov • You • Founder...")
      // if present. Heuristic: first newline is the boundary between the
      // meta header and the comment body.
      const lines = text.split('\n');
      while (lines.length > 1) {
        const head = lines[0];
        if (/Matthew Diakonov|• You|Founder|\d+[hdmw]\s*$/i.test(head)) {
          lines.shift();
        } else {
          break;
        }
      }
      text = lines.join('\n').trim();

      const prev = acc.get(comment_id);
      if (!prev) added++;
      acc.set(comment_id, {
        comment_id, parent_kind, parent_id,
        comment_text: text || (prev ? prev.comment_text : ''),
      });
    });
    return added;
  }

  let ticks = 0;
  let stagnant = 0;
  let lastH = document.documentElement.scrollHeight;

  const tick = () => {
    const added = harvest();
    const h = document.documentElement.scrollHeight;
    ticksLog.push({tick: ticks, added, total: acc.size, scroll_height: h});

    if (added === 0 && h === lastH) stagnant++;
    else stagnant = 0;
    lastH = h;

    // Scroll to NEAR the bottom of the page. LinkedIn's infinite-scroll
    // sentinel fires when the viewport approaches the page foot, not
    // when you scrollBy() by a fixed delta mid-page. Leave a small
    // 200px buffer so we don't overshoot past the sentinel.
    window.scrollTo(0, document.documentElement.scrollHeight - 200);
    ticks++;

    const wait = opts.pause_min_ms
               + Math.random() * (opts.pause_max_ms - opts.pause_min_ms);

    if (ticks < opts.max_scrolls && stagnant < opts.stagnant_tol) {
      setTimeout(tick, wait);
    } else {
      setTimeout(() => {
        harvest();
        resolve({
          records: [...acc.values()],
          ticks,
          stagnant,
          scroll_height_final: document.documentElement.scrollHeight,
          ticks_log: ticksLog,
        });
      }, opts.settle_ms);
    }
  };
  tick();
});
"""


def do_scrape(out_path: str, max_scrolls: int) -> dict:
    """Phase 1: deep-scroll the activity feed and capture (urn, text) per article."""
    # Reuse the existing CDP-attach helper that the stats scraper uses.
    from linkedin_browser import (
        _acquire_browser_lock, _connect_to_running_or_launch,
        _is_login_or_checkpoint,
    )
    from playwright.sync_api import sync_playwright

    _acquire_browser_lock()

    with sync_playwright() as p:
        try:
            context, owns_context = _connect_to_running_or_launch(p)
        except Exception as e:
            return {"ok": False, "error": "profile_locked", "detail": str(e)}

        page = None
        try:
            page = context.new_page()
            try:
                page.goto(COMMENTS_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                return {"ok": False, "error": "navigation_failed", "detail": str(e)}

            try:
                page.wait_for_selector("article, main", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(2500)

            if _is_login_or_checkpoint(page.url or ""):
                return {"ok": False, "error": "session_invalid", "url": page.url}

            try:
                result = page.evaluate(HARVEST_JS, {
                    "max_scrolls": int(max_scrolls),
                    "pause_min_ms": SCROLL_PAUSE_MIN_MS,
                    "pause_max_ms": SCROLL_PAUSE_MAX_MS,
                    "dy_min": SCROLL_DY_MIN,
                    "dy_max": SCROLL_DY_MAX,
                    "settle_ms": HARVEST_SETTLE_MS,
                    "stagnant_tol": STAGNANT_TOLERANCE,
                })
            except Exception as e:
                return {"ok": False, "error": "evaluate_failed", "detail": str(e)}

            records = result.get("records") or []
            with_text = sum(1 for r in records if (r.get("comment_text") or "").strip())
            with open(out_path, "w") as f:
                json.dump(records, f)

            return {
                "ok": True,
                "url": page.url,
                "ticks": result.get("ticks"),
                "stagnant_at_stop": result.get("stagnant"),
                "scroll_height_final": result.get("scroll_height_final"),
                "record_count": len(records),
                "with_text": with_text,
                "out": out_path,
            }
        finally:
            if page is not None:
                try: page.close()
                except Exception: pass
            if owns_context:
                try: context.close()
                except Exception: pass


def _norm(s: Optional[str]) -> str:
    """Whitespace-normalize a string for content matching."""
    if not s: return ""
    return " ".join(s.split())


def do_match_apply(in_path: str, apply_writes: bool, quiet: bool) -> dict:
    """Phase 2/3: match scraped records vs DB, apply URL updates."""
    import db as dbmod
    dbmod.load_env()
    db = dbmod.get_conn()

    feed = json.load(open(in_path))
    if not isinstance(feed, list):
        raise ValueError("feed must be a list of records")

    # Pull the candidate posts rows. Legacy = our_url is a LinkedIn
    # /feed/update/ URL with NO commentUrn already attached.
    cur = db.execute("""
        SELECT id, our_url, thread_url, our_content, posted_at
        FROM posts
        WHERE platform='linkedin'
          AND our_url IS NOT NULL
          AND our_url ILIKE '%linkedin.com/feed/update/urn:li:%'
          AND our_url NOT ILIKE '%commentUrn%'
    """)
    candidates = cur.fetchall()
    if not quiet:
        print(f"[backfill] feed={len(feed)} candidate_posts={len(candidates)}", flush=True)

    # Build content -> [posts.id] index for fast lookup. Key on the first
    # 60 chars (whitespace-normalized, lowercased) for fuzzy-but-tight match.
    by_content = {}
    for c in candidates:
        key = _norm(c["our_content"] or "").lower()[:60]
        if not key: continue
        by_content.setdefault(key, []).append(c)

    matched = 0
    ambiguous = 0
    no_match = 0
    updates = []

    for fr in feed:
        cid = fr.get("comment_id")
        text = _norm(fr.get("comment_text") or "")
        if not text or not cid:
            no_match += 1
            continue
        key = text.lower()[:60]
        rows = by_content.get(key) or []
        if len(rows) == 0:
            no_match += 1
            continue
        if len(rows) > 1:
            ambiguous += 1
            continue

        row = rows[0]
        # Build the new our_url. parent_kind from feed tells us which
        # namespace to use; reconstruct the inner URN.
        parent_kind = fr["parent_kind"]
        parent_id = fr["parent_id"]
        comment_urn = f"urn:li:comment:(urn:li:{parent_kind}:{parent_id},{cid})"
        encoded = urllib.parse.quote(comment_urn, safe="")
        base = row["thread_url"].rstrip("/")
        new_url = f"{base}/?commentUrn={encoded}"
        updates.append((row["id"], row["our_url"], new_url))
        matched += 1

    if not quiet:
        print(f"[backfill] matched={matched} ambiguous={ambiguous} no_match={no_match}", flush=True)
        for pid, old, new in updates[:5]:
            print(f"  posts.id={pid}")
            print(f"    old: {(old or '')[:100]}")
            print(f"    new: {new[:100]}...")

    if apply_writes and updates:
        for pid, _, new in updates:
            db.execute("UPDATE posts SET our_url = %s WHERE id = %s", [new, pid])
        db.commit()
        if not quiet:
            print(f"[backfill] APPLIED {len(updates)} updates", flush=True)
    elif apply_writes:
        if not quiet:
            print("[backfill] no updates to apply", flush=True)
    else:
        if not quiet:
            print("[backfill] dry-run; no writes performed", flush=True)

    db.close()
    return {
        "ok": True,
        "feed_size": len(feed),
        "candidates": len(candidates),
        "matched": matched,
        "ambiguous": ambiguous,
        "no_match": no_match,
        "applied": len(updates) if apply_writes else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scrape", action="store_true",
                    help="Phase 1: scrape the activity feed.")
    ap.add_argument("--match", action="store_true",
                    help="Phase 2: match feed vs DB (read-only).")
    ap.add_argument("--apply", action="store_true",
                    help="Phase 3: apply URL updates to DB.")
    ap.add_argument("--out", default=None, help="Output JSON path (for --scrape).")
    ap.add_argument("--in", dest="in_path", default=None,
                    help="Input JSON path (for --match / --apply).")
    ap.add_argument("--max-scrolls", type=int, default=80)
    ap.add_argument("--dry-run", action="store_true",
                    help="With --match, suppress writes (default already read-only).")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.scrape:
        if os.environ.get("SOCIAL_AUTOPOSTER_LINKEDIN_BACKFILL") != "1":
            print(json.dumps({"ok": False, "error": "unauthorized_caller",
                              "detail": "Set SOCIAL_AUTOPOSTER_LINKEDIN_BACKFILL=1."}),
                  file=sys.stderr)
            sys.exit(2)
        if not args.out:
            print("--out required with --scrape", file=sys.stderr)
            sys.exit(1)
        res = do_scrape(args.out, args.max_scrolls)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("ok") else 1)

    if args.match or args.apply:
        if not args.in_path:
            print("--in required", file=sys.stderr)
            sys.exit(1)
        res = do_match_apply(args.in_path,
                             apply_writes=(args.apply and not args.dry_run),
                             quiet=args.quiet)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("ok") else 1)

    print("Specify one of --scrape / --match / --apply", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
