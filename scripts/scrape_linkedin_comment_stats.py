#!/usr/bin/env python3
"""LinkedIn comment-stats scraper: read-only DOM harvest, no LLM.

Replaces the old `claude -p` driven `stats-linkedin-comments.sh` body.
That version cost $0.10-0.30 per fire (skill + prompt + tool schemas
through the model) for work that is 100% deterministic. This script
does the same harvest with zero token cost.

Per CLAUDE.md "LinkedIn: flagged patterns" carve-out (2026-04-29):
read-only DOM scrapes via Python Playwright are allowed when they
match the linkedin_browser.py shape:
    - Headed Chromium (not headless; LinkedIn fingerprints headless).
    - Persistent profile inheritance from linkedin-agent.
    - ONE page.goto per invocation.
    - ONE page.evaluate; no clicks, no permalink hops, no Voyager API.
    - Programmatic login forbidden; SESSION_INVALID and stop instead.

The 2026-04-17 LinkedIn restriction was caused by Voyager API calls +
per-permalink scroll-and-expand loops, NOT by Python existing in the
call stack. This helper has neither.

Usage:
    SOCIAL_AUTOPOSTER_LINKEDIN_COMMENT_STATS=1 \\
    python3 scrape_linkedin_comment_stats.py [--out PATH] [--max-scrolls N]

Output (JSON written to --out path AND echoed to stdout):
    {
        "ok": true,
        "url": "https://www.linkedin.com/in/me/recent-activity/comments/",
        "scrolled_ticks": 40,
        "scroll_height_final": 18234,
        "records": [
            {"comment_id": "...", "parent_kind": "ugcPost",
             "parent_id": "...", "impressions": 156,
             "reactions": 7, "replies": 1},
            ...
        ],
        "record_count": 23,
        "with_impressions": 19,
        "with_reactions": 14
    }

Failure shapes:
    {"ok": false, "error": "session_invalid", "url": "..."}
    {"ok": false, "error": "wrong_page", "url": "...", "title": "..."}
    {"ok": false, "error": "captcha_or_checkpoint", "detail": "..."}
    {"ok": false, "error": "navigation_failed", "detail": "..."}
    {"ok": false, "error": "profile_locked", "detail": "..."}
    {"ok": false, "error": "evaluate_failed", "detail": "..."}
    {"ok": false, "error": "exception", "detail": "..."}

Exit 0 on ok, 1 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

# Reuse the shared lock + login-detector + profile constants from
# linkedin_browser.py so concurrent helpers (unread-dms, comment stats,
# SERP discovery) all serialize on the same lock file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linkedin_browser import (  # noqa: E402
    LOCK_POLL_INTERVAL,
    LOCK_WAIT_MAX,
    PROFILE_DIR,
    SYSTEM_CHROME,
    VIEWPORT,
    _acquire_browser_lock,
    _connect_to_running_or_launch,
    _is_login_or_checkpoint,
)


COMMENTS_URL = "https://www.linkedin.com/in/me/recent-activity/comments/"

# Tunables (also passable via CLI flags).
DEFAULT_MAX_SCROLLS = 40
SCROLL_PAUSE_MIN_MS = 1800
SCROLL_PAUSE_MAX_MS = 3500
SCROLL_DY_MIN = 600
SCROLL_DY_MAX = 1100
HARVEST_SETTLE_MS = 1500


# JS executed inside ONE page.evaluate(). Does the slow scroll +
# harvest-during-scroll into an accumulator keyed by comment_id.
# LinkedIn virtualizes the comments tab aggressively (articles get
# detached when they leave the viewport), so an end-only harvest
# would miss everything but the bottom slice. We harvest before each
# scroll, accumulating into a Map.
HARVEST_JS_TEMPLATE = r"""
(opts) => new Promise(resolve => {
  const acc = new Map();
  const ticksLog = [];

  function harvest() {
    let added_this_tick = 0;
    document.querySelectorAll('article').forEach(art => {
      const urnEl = art.querySelector(
        '[data-urn^="urn:li:comment:"], [data-id^="urn:li:comment:"]'
      );
      if (!urnEl) return;
      const urn = urnEl.getAttribute('data-urn')
                || urnEl.getAttribute('data-id') || '';
      const m = urn.match(/^urn:li:comment:\((\w+):(\d+),(\d+)\)$/);
      if (!m) return;
      const parent_kind = m[1], parent_id = m[2], comment_id = m[3];

      let impressions = null, reactions = null, replies = null;
      let saw_like = false, saw_reply = false;

      art.querySelectorAll('div, span, p, button, a').forEach(leaf => {
        if (leaf.children.length > 0) return;
        const t = (leaf.innerText || '').trim();
        if (!t) return;
        if (impressions === null) {
          const x = t.match(/^([\d,]+)\s+impressions?$/i);
          if (x) impressions = parseInt(x[1].replace(/,/g,''));
        }
        if (replies === null) {
          const x = t.match(/^([\d,]+)\s+repl(y|ies)$/i);
          if (x) replies = parseInt(x[1].replace(/,/g,''));
        }
        if (t === 'Like')  saw_like  = true;
        if (t === 'Reply') saw_reply = true;
      });

      // Reactions: aria-label of the count button. LinkedIn omits the
      // count when reactions=0 (no button at all), which is why we fall
      // back to 0 only when both Like and Reply leaves are present (a
      // signal that the comment IS rendered, just has zero reactions).
      for (const b of art.querySelectorAll('button[aria-label*="eaction"]')) {
        const lbl = b.getAttribute('aria-label') || '';
        const x = lbl.match(/^([\d,]+)\s+Reaction/i);
        if (x) { reactions = parseInt(x[1].replace(/,/g,'')); break; }
      }
      if (reactions === null && saw_like && saw_reply) reactions = 0;
      if (replies   === null && saw_reply)             replies   = 0;

      const prev = acc.get(comment_id);
      if (!prev) added_this_tick++;
      acc.set(comment_id, {
        comment_id, parent_kind, parent_id,
        impressions: (impressions !== null ? impressions
                       : (prev ? prev.impressions : null)),
        reactions:   (reactions   !== null ? reactions
                       : (prev ? prev.reactions   : null)),
        replies:     (replies     !== null ? replies
                       : (prev ? prev.replies     : null)),
      });
    });
    return added_this_tick;
  }

  let ticks = 0;
  let stagnant = 0;  // consecutive ticks with no new comments
  let lastScrollHeight = document.documentElement.scrollHeight;

  const tick = () => {
    const added = harvest();
    const sh = document.documentElement.scrollHeight;
    ticksLog.push({tick: ticks, added, total: acc.size,
                   scroll_height: sh});

    // Early-stop if list has stabilized and we've stopped finding new
    // comments. Saves time + avoids hammering the lazy-loader past its
    // wall.
    if (added === 0 && sh === lastScrollHeight) {
      stagnant++;
    } else {
      stagnant = 0;
    }
    lastScrollHeight = sh;

    const dy = opts.dy_min + Math.random() * (opts.dy_max - opts.dy_min);
    window.scrollBy(0, dy);
    ticks++;

    const wait = opts.pause_min_ms
               + Math.random() * (opts.pause_max_ms - opts.pause_min_ms);

    if (ticks < opts.max_scrolls && stagnant < 4) {
      setTimeout(tick, wait);
    } else {
      // Final settle + harvest.
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


def _looks_like_captcha_or_checkpoint(page) -> Optional[str]:
    """Best-effort heuristic for LinkedIn challenge pages.

    Returns a short description string if we suspect a challenge
    (captcha, checkpoint, "let's confirm it's you"), else None.
    """
    try:
        url = page.url or ""
        if _is_login_or_checkpoint(url):
            return f"login_or_checkpoint_url:{url}"

        # Title heuristic.
        try:
            title = (page.title() or "").lower()
        except Exception:
            title = ""
        if any(s in title for s in ("security verification",
                                    "let's do a quick security check",
                                    "let us do a security check",
                                    "checkpoint")):
            return f"title:{title}"

        # Body-text heuristic. Read first ~400 chars of <body> innerText.
        try:
            body = page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 400)"
            ) or ""
        except Exception:
            body = ""
        body_l = body.lower()
        for marker in (
            "let's do a quick security check",
            "let us do a quick security check",
            "verify you're a human",
            "we want to make sure",
            "press and hold",
            "we couldn't verify",
            "captcha",
        ):
            if marker in body_l:
                return f"body:{marker}"
    except Exception:
        return None
    return None


def _comments_tab_present(page) -> bool:
    """Confirm we landed on the Comments tab and not somewhere else.

    Heuristic: the comments tab renders <article> elements with
    data-urn="urn:li:comment:..." and an "X impressions" leaf. If
    EITHER of those is present, we're on the right page. We accept
    "no impressions yet" as long as comment URNs exist (fresh user).
    """
    try:
        sig = page.evaluate(
            """() => {
              const urns = document.querySelectorAll(
                '[data-urn^="urn:li:comment:"], [data-id^="urn:li:comment:"]'
              ).length;
              const imps = (document.body && document.body.innerText || '')
                            .match(/\\d+\\s+impressions?/g);
              return {
                urns,
                impression_leaves: imps ? imps.length : 0,
              };
            }"""
        ) or {}
        return bool(sig.get("urns") or sig.get("impression_leaves"))
    except Exception:
        return False


def scrape(out_path: Optional[str], max_scrolls: int) -> dict:
    """Run the scrape. Returns result dict.

    2026-05-08: switched from launch_persistent_context (which forced
    skill/stats-linkedin-comments.sh to first SIGKILL the linkedin-agent
    MCP Chrome via ensure_browser_healthy, producing a kill+reopen
    cadence that LinkedIn anti-bot flagged on 2026-05-06) to a
    CDP-attach via _connect_to_running_or_launch. New tabs land in the
    existing MCP Chrome's BrowserContext, so cookies/fingerprint match
    perfectly and no second Chrome process is ever spawned. The
    launch_persistent_context fallback inside the helper still exists
    for the cold-MCP case.
    """
    from playwright.sync_api import sync_playwright

    _acquire_browser_lock()

    with sync_playwright() as p:
        try:
            context, owns_context = _connect_to_running_or_launch(p)
        except Exception as e:
            return {
                "ok": False,
                "error": "profile_locked",
                "detail": str(e),
            }

        page = None
        try:
            page = context.new_page()
            try:
                page.goto(
                    COMMENTS_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                return {
                    "ok": False,
                    "error": "navigation_failed",
                    "detail": str(e),
                }

            # Settle.
            try:
                page.wait_for_selector(
                    "article, main",
                    timeout=10000,
                )
            except Exception:
                pass
            page.wait_for_timeout(2500)

            cur_url = page.url
            if _is_login_or_checkpoint(cur_url):
                return {
                    "ok": False,
                    "error": "session_invalid",
                    "url": cur_url,
                }

            challenge = _looks_like_captcha_or_checkpoint(page)
            if challenge:
                return {
                    "ok": False,
                    "error": "captcha_or_checkpoint",
                    "url": cur_url,
                    "detail": challenge,
                }

            if not _comments_tab_present(page):
                # Page loaded but isn't the comments tab. Could be
                # rate-limit landing page, A/B-tested redesign that
                # broke our selectors, or a soft 404.
                try:
                    title = page.title() or ""
                except Exception:
                    title = ""
                return {
                    "ok": False,
                    "error": "wrong_page",
                    "url": cur_url,
                    "title": title,
                }

            # ONE harvest evaluate. Internal scroll loop runs there.
            try:
                result = page.evaluate(
                    HARVEST_JS_TEMPLATE,
                    {
                        "max_scrolls": int(max_scrolls),
                        "pause_min_ms": SCROLL_PAUSE_MIN_MS,
                        "pause_max_ms": SCROLL_PAUSE_MAX_MS,
                        "dy_min": SCROLL_DY_MIN,
                        "dy_max": SCROLL_DY_MAX,
                        "settle_ms": HARVEST_SETTLE_MS,
                    },
                )
            except Exception as e:
                return {
                    "ok": False,
                    "error": "evaluate_failed",
                    "detail": str(e),
                }

            records = result.get("records") or []
            with_imp = sum(
                1 for r in records if r.get("impressions") is not None
            )
            with_rxn = sum(
                1 for r in records if r.get("reactions") is not None
            )

            out = {
                "ok": True,
                "url": cur_url,
                "scrolled_ticks": result.get("ticks", 0),
                "stagnant_ticks_at_stop": result.get("stagnant", 0),
                "scroll_height_final": result.get("scroll_height_final", 0),
                "records": records,
                "record_count": len(records),
                "with_impressions": with_imp,
                "with_reactions": with_rxn,
                "ticks_log": result.get("ticks_log", []),
            }

            if out_path:
                # Write the records-only JSON in the shape that
                # update_linkedin_comment_stats_from_feed.py expects.
                try:
                    with open(out_path, "w") as f:
                        json.dump(records, f)
                except Exception as e:
                    out["write_warning"] = (
                        f"failed to write {out_path}: {e}"
                    )

            return out
        finally:
            # Always close OUR page so the MCP Chrome doesn't accumulate
            # tabs across fires.
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            # Only close the context when we own it (cold-MCP fallback
            # path). When CDP-attached to the linkedin-agent MCP, the
            # context belongs to that MCP and closing it terminates the
            # MCP's Chrome — exactly the kill+reopen cadence we are
            # trying to eliminate.
            if owns_context:
                try:
                    context.close()
                except Exception:
                    pass


def main():
    if os.environ.get("SOCIAL_AUTOPOSTER_LINKEDIN_COMMENT_STATS") != "1":
        print(
            json.dumps({
                "ok": False,
                "error": "unauthorized_caller",
                "detail": (
                    "scrape_linkedin_comment_stats.py is invoked only by "
                    "stats-linkedin-comments.sh. Set "
                    "SOCIAL_AUTOPOSTER_LINKEDIN_COMMENT_STATS=1 from the "
                    "caller if this invocation is legitimate."
                ),
            }),
            file=sys.stderr,
        )
        sys.exit(2)

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None,
                    help="Path to write feed JSON (records-only array). "
                         "If omitted, only stdout summary is produced.")
    ap.add_argument("--max-scrolls", type=int, default=DEFAULT_MAX_SCROLLS,
                    help=f"Max scroll ticks (default {DEFAULT_MAX_SCROLLS}).")
    args = ap.parse_args()

    try:
        result = scrape(args.out, args.max_scrolls)
    except Exception as e:
        result = {
            "ok": False,
            "error": "exception",
            "detail": f"{type(e).__name__}: {e}",
        }

    # Strip the verbose ticks_log from stdout (logs file get the full one
    # via --out). Keep the summary fields useful for shell-side parsing.
    stdout_view = {k: v for k, v in result.items() if k != "ticks_log"}
    if "records" in stdout_view:
        # drop record bodies from stdout to keep launchd log compact
        stdout_view["records"] = f"<{len(stdout_view['records'])} records>"
    print(json.dumps(stdout_view, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
