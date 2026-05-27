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
    {"ok": false, "error": "early_stop_no_records",
                            "early_stop_reason": "..."}
    {"ok": false, "error": "navigation_failed", "detail": "..."}
    {"ok": false, "error": "profile_locked", "detail": "..."}
    {"ok": false, "error": "evaluate_failed", "detail": "..."}
    {"ok": false, "error": "exception", "detail": "..."}

Partial-success shape (records harvested before a challenge fired
mid-scroll). 2026-05-26: added so the writer can still apply real
stats deltas instead of dropping a whole fire's worth of work on a
late-injected captcha:
    {"ok": true, "partial": true,
     "early_stop_reason": "title:security verification | url:.../checkpoint",
     "records": [...], "record_count": N, ...}

Exit 0 on ok (including partial), 1 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
import traceback
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Debug-bundle helpers (added 2026-05-26 after the 2026-05-19 session_invalid
# event left only 14 lines of orchestrator log to debug from).
#
# When --debug-dir is set, the scraper writes a forensic bundle for every
# fire (success or failure), then tars it up. The shell caller (stats-
# linkedin.sh) promotes the tarball to a permanent archive on session_
# invalid / captcha_or_checkpoint so we can compare the next failure DOM
# against the last-known-good one byte-for-byte. On success the bundle
# stays in skill/logs/linkedin-debug/<ts>/ on disk for 14 days then ages
# out via stats-linkedin.sh's existing find -mtime sweep.
#
# Every helper here is wrapped so a debug-side failure can NEVER raise into
# the main scrape() path. The whole point is fault diagnosis; a diagnostics
# helper that crashes the production run would be worse than no helper.
# ---------------------------------------------------------------------------


def _ts_ms() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _DebugRecorder:
    """Sink for forensic artifacts captured during one scrape() invocation.

    Files written under self.dir (one bundle per fire):
        00_owns_context.txt    cdp_attach vs cold_launch (post-attach)
        00_chrome_version.txt  browser.version + platform info
        01_pre_goto.png        screenshot before page.goto
        01_pre_goto.html       outerHTML before page.goto
        02_post_goto.png       screenshot after page.goto + settle
        02_post_goto.html      outerHTML after page.goto + settle
        02_post_goto_url.txt   page.url after goto (the smoking-gun for
                               session_invalid: shows /authwall URL)
        02_cookies.json        full cookie jar (li_at, JSESSIONID, etc.)
        99_failure.png         screenshot at error-return path
        99_failure.html        outerHTML at error-return path
        99_failure.txt         error/detail + Python traceback
        console.jsonl          page console messages + uncaught pageerrors
        navigation.jsonl       framenavigated events (ALL frames; the
                               authwall redirect chain is the data here)
        network.jsonl          response events for *.linkedin.com requests
                               (status, url, content-type; body truncated
                               to 2KB to keep bundle tractable)
        meta.json              start/end timestamps + scrape summary

    Disable globally by passing debug_dir=None to scrape(). The instance
    becomes a no-op shim — all `dbg.x(...)` calls return None instantly.
    """

    def __init__(self, debug_dir: Optional[str]):
        self.dir: Optional[str] = debug_dir
        self.enabled: bool = bool(debug_dir)
        self.started_at: str = _ts_ms()
        self.meta: dict = {}
        # Open file handles (append) for the streaming sinks. Lazy so we
        # don't create empty files when the recorder is disabled.
        self._fh_console = None
        self._fh_nav = None
        self._fh_net = None
        if self.enabled:
            try:
                os.makedirs(self.dir, exist_ok=True)
            except OSError as e:
                # If we can't make the dir, drop to no-op.
                print(
                    f"[scrape_linkedin] WARN: debug dir create failed "
                    f"({e!r}); disabling debug capture",
                    file=sys.stderr,
                    flush=True,
                )
                self.enabled = False
                self.dir = None

    # --- low-level writers ------------------------------------------------

    def _path(self, name: str) -> Optional[str]:
        if not self.enabled or not self.dir:
            return None
        return os.path.join(self.dir, name)

    def _write_text(self, name: str, body: str) -> None:
        p = self._path(name)
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8", errors="replace") as f:
                f.write(body)
        except OSError as e:
            print(
                f"[scrape_linkedin] WARN: debug write {name} failed: {e!r}",
                file=sys.stderr,
                flush=True,
            )

    def _append_jsonl(self, handle_name: str, name: str, obj: dict) -> None:
        if not self.enabled:
            return
        fh = getattr(self, handle_name)
        if fh is None:
            p = self._path(name)
            if not p:
                return
            try:
                fh = open(p, "a", encoding="utf-8", errors="replace")
            except OSError as e:
                print(
                    f"[scrape_linkedin] WARN: debug open {name} failed: "
                    f"{e!r}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            setattr(self, handle_name, fh)
        try:
            fh.write(json.dumps(obj, default=str) + "\n")
            fh.flush()
        except (OSError, TypeError, ValueError):
            # Never let a jsonl write derail the scrape.
            pass

    def _close_handles(self) -> None:
        for attr in ("_fh_console", "_fh_nav", "_fh_net"):
            fh = getattr(self, attr, None)
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
                setattr(self, attr, None)

    # --- public capture API ----------------------------------------------

    def note_owns_context(self, owns_context: bool) -> None:
        if not self.enabled:
            return
        line = (
            f"owns_context={owns_context}\n"
            f"meaning="
            f"{'cold_launch_persistent_context' if owns_context else 'cdp_attach_to_running_mcp'}\n"
            f"profile={PROFILE_DIR}\n"
            f"pid={os.getpid()}\n"
            f"timestamp={_ts_ms()}\n"
        )
        self._write_text("00_owns_context.txt", line)

    def capture_browser_version(self, context) -> None:
        if not self.enabled or context is None:
            return
        info = {}
        try:
            br = getattr(context, "browser", None)
            if br is not None:
                info["browser_version"] = getattr(br, "version", "?")
                info["browser_type"] = (
                    br.browser_type.name if getattr(br, "browser_type", None)
                    else "?"
                )
        except Exception as e:
            info["browser_version_err"] = repr(e)
        info["sys.platform"] = sys.platform
        info["py"] = sys.version.split()[0]
        info["captured_at"] = _ts_ms()
        try:
            body = "\n".join(f"{k}={v}" for k, v in info.items())
        except Exception:
            body = repr(info)
        self._write_text("00_chrome_version.txt", body + "\n")

    def attach_page_listeners(self, page) -> None:
        """Subscribe to page events. Must be called BEFORE page.goto."""
        if not self.enabled or page is None:
            return

        def on_console(msg):
            try:
                rec = {
                    "ts": _ts_ms(),
                    "kind": "console",
                    "type": msg.type,
                    "text": (msg.text or "")[:4000],
                    "location": getattr(msg, "location", None),
                }
            except Exception as e:
                rec = {"ts": _ts_ms(), "kind": "console", "err": repr(e)}
            self._append_jsonl("_fh_console", "console.jsonl", rec)

        def on_pageerror(err):
            try:
                rec = {
                    "ts": _ts_ms(),
                    "kind": "pageerror",
                    "name": getattr(err, "name", type(err).__name__),
                    "message": (str(err) or "")[:4000],
                    "stack": (getattr(err, "stack", "") or "")[:4000],
                }
            except Exception as e:
                rec = {"ts": _ts_ms(), "kind": "pageerror", "err": repr(e)}
            self._append_jsonl("_fh_console", "console.jsonl", rec)

        def on_framenav(frame):
            try:
                rec = {
                    "ts": _ts_ms(),
                    "url": frame.url,
                    "name": frame.name,
                    "is_main": frame == page.main_frame,
                }
            except Exception as e:
                rec = {"ts": _ts_ms(), "err": repr(e)}
            self._append_jsonl("_fh_nav", "navigation.jsonl", rec)

        def on_response(response):
            # LinkedIn-only: keeps bundle <1MB on a typical run.
            try:
                url = response.url
                if "linkedin.com" not in url:
                    return
                # Rate-limit canary. LinkedIn rarely returns a bare 429 —
                # it usually redirects to /authwall or injects a captcha
                # overlay (both caught by the in-JS detectChallengeInDom
                # gate). But when a raw 429 does fire, surface it as a
                # grep-able stderr marker so the orchestrator log shows
                # the canary even when the run continues. Also stamp
                # meta.json so the in-bundle summary records it.
                if response.status == 429:
                    print(
                        f"[scrape_linkedin] saw_429 url={url[:200]}",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        self.meta.setdefault("saw_429", []).append({
                            "ts": _ts_ms(), "url": url[:200],
                        })
                    except Exception:
                        pass
                rec = {
                    "ts": _ts_ms(),
                    "status": response.status,
                    "url": url,
                    "method": response.request.method,
                    "type": response.request.resource_type,
                    "headers": dict(list(response.headers.items())[:30]),
                }
                # Only capture body for HTML/JSON and only first 2KB; full
                # response bodies blow up the tarball with no diagnostic
                # win over the URL + status.
                ct = (response.headers.get("content-type") or "").lower()
                if response.status >= 300 and ("html" in ct or "json" in ct
                                              or ct == ""):
                    try:
                        body = response.text()
                        rec["body_snip"] = (body or "")[:2048]
                    except Exception:
                        pass
            except Exception as e:
                rec = {"ts": _ts_ms(), "err": repr(e)}
            self._append_jsonl("_fh_net", "network.jsonl", rec)

        try:
            page.on("console", on_console)
            page.on("pageerror", on_pageerror)
            page.on("framenavigated", on_framenav)
            page.on("response", on_response)
        except Exception as e:
            print(
                f"[scrape_linkedin] WARN: page.on subscribe failed: {e!r}",
                file=sys.stderr,
                flush=True,
            )

    def snapshot(self, page, prefix: str) -> None:
        """Write <prefix>.png + <prefix>.html for the given page."""
        if not self.enabled or page is None:
            return
        # screenshot
        png_path = self._path(f"{prefix}.png")
        if png_path:
            try:
                page.screenshot(path=png_path, full_page=False, timeout=8000)
            except Exception as e:
                self._write_text(
                    f"{prefix}.png.err.txt",
                    f"screenshot_failed: {e!r}\nts={_ts_ms()}\n",
                )
        # outerHTML
        try:
            html = page.content()
            self._write_text(f"{prefix}.html", html)
        except Exception as e:
            self._write_text(
                f"{prefix}.html.err.txt",
                f"content_read_failed: {e!r}\nts={_ts_ms()}\n",
            )

    def capture_url(self, page, prefix: str) -> None:
        if not self.enabled or page is None:
            return
        try:
            url = page.url
        except Exception as e:
            url = f"<url_read_failed: {e!r}>"
        self._write_text(
            f"{prefix}_url.txt", f"{url}\nts={_ts_ms()}\n"
        )

    def capture_cookies(self, context, prefix: str = "02_cookies") -> None:
        if not self.enabled or context is None:
            return
        try:
            cookies = context.cookies()
        except Exception as e:
            self._write_text(
                f"{prefix}.err.txt",
                f"cookies_read_failed: {e!r}\nts={_ts_ms()}\n",
            )
            return
        # Don't redact li_at / JSESSIONID: this is a private bundle stored
        # on the user's machine; the same cookies are sitting in the same
        # profile dir on disk anyway. Their presence / absence / age IS
        # the diagnostic signal for session_invalid.
        try:
            self._write_text(
                f"{prefix}.json",
                json.dumps(cookies, indent=2, default=str),
            )
        except Exception as e:
            self._write_text(
                f"{prefix}.err.txt",
                f"cookies_serialize_failed: {e!r}\nts={_ts_ms()}\n",
            )

    def failure(self, page, error: str, detail: str = "") -> None:
        """Capture failure-mode artifacts: screenshot, html, error text."""
        if not self.enabled:
            return
        self.snapshot(page, "99_failure")
        try:
            url = page.url if page is not None else "<no_page>"
        except Exception:
            url = "<url_read_failed>"
        body = (
            f"error={error}\n"
            f"detail={detail}\n"
            f"url={url}\n"
            f"ts={_ts_ms()}\n"
            f"\n--- python traceback ---\n"
            f"{traceback.format_exc()}"
        )
        self._write_text("99_failure.txt", body)

    def finalize(self, result: dict) -> Optional[str]:
        """Write meta.json, close jsonl handles, tar.gz the dir.

        Returns absolute path to the .tar.gz on success, None on failure
        or when disabled. The shell caller surfaces this path in its log
        and (on session_invalid) promotes it to a permanent archive.
        """
        if not self.enabled or not self.dir:
            return None
        self.meta["started_at"] = self.started_at
        self.meta["finished_at"] = _ts_ms()
        self.meta["pid"] = os.getpid()
        self.meta["ok"] = bool(result.get("ok"))
        self.meta["error"] = result.get("error")
        self.meta["records"] = result.get("record_count")
        self.meta["with_impressions"] = result.get("with_impressions")
        self.meta["with_reactions"] = result.get("with_reactions")
        try:
            self._write_text(
                "meta.json",
                json.dumps(self.meta, indent=2, default=str),
            )
        except Exception:
            pass

        self._close_handles()

        # Tar the directory next to itself: <dir>.tar.gz
        tarball = self.dir.rstrip("/") + ".tar.gz"
        try:
            with tarfile.open(tarball, "w:gz") as tar:
                tar.add(self.dir, arcname=os.path.basename(self.dir))
        except Exception as e:
            print(
                f"[scrape_linkedin] WARN: tarball create failed: {e!r}",
                file=sys.stderr,
                flush=True,
            )
            return None
        return tarball


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
      // Accept BOTH the bare-kind form `urn:li:comment:(ugcPost:X,Y)`
      // (current LinkedIn DOM) and the fully-qualified form
      // `urn:li:comment:(urn:li:ugcPost:X,Y)` (legacy / Voyager-derived).
      // The `(?:urn:li:)?` non-capturing group makes the inner prefix
      // optional so we don't silently drop articles if LinkedIn switches
      // formats. Mirror of the Python regex fix in
      // update_linkedin_comment_stats_from_feed.py (2026-05-11).
      const m = urn.match(/^urn:li:comment:\((?:urn:li:)?(\w+):(\d+),(\d+)\)$/);
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

  // Mid-scrape challenge detector. Pre-loop gates in Python catch the
  // URL-redirect form (LinkedIn 302's to /authwall on stale session).
  // This catches the DOM-overlay + title-change + URL-mutate forms
  // LinkedIn can inject BETWEEN ticks (rate-limit captcha, security
  // verification splash, "let's confirm it's you"). On detect, the
  // tick loop breaks NOW and resolves with whatever records have been
  // accumulated so far, plus an early_stop_reason so Python can mark
  // the result partial and still feed records into the writer.
  function detectChallengeInDom() {
    try {
      const u = (location.href || '').toLowerCase();
      if (u.indexOf('/authwall') !== -1
          || u.indexOf('/checkpoint') !== -1
          || u.indexOf('/uas/login') !== -1) {
        return 'url:' + u.slice(0, 200);
      }
      const title = (document.title || '').toLowerCase();
      if (title.indexOf('security verification') !== -1
          || title.indexOf('checkpoint') !== -1
          || title.indexOf("let's do a quick") !== -1) {
        return 'title:' + title.slice(0, 200);
      }
      const body = ((document.body && document.body.innerText) || '')
                    .slice(0, 400).toLowerCase();
      const bodyMarkers = ["let's do a quick security check",
                           "let us do a quick security check",
                           "verify you're a human",
                           "press and hold",
                           "we couldn't verify",
                           "we want to make sure",
                           "captcha"];
      for (let i = 0; i < bodyMarkers.length; i++) {
        if (body.indexOf(bodyMarkers[i]) !== -1) {
          return 'body:' + bodyMarkers[i];
        }
      }
    } catch (e) {
      return null;
    }
    return null;
  }

  let ticks = 0;
  let stagnant = 0;  // consecutive ticks with no new comments
  let lastScrollHeight = document.documentElement.scrollHeight;

  const tick = () => {
    // Mid-scrape gate. If LinkedIn injected a challenge between ticks
    // (captcha overlay, /checkpoint redirect, "security verification"),
    // stop NOW with whatever we've already harvested rather than
    // hammering through the wall. Partial > zero.
    const challenge = detectChallengeInDom();
    if (challenge) {
      // Final best-effort harvest before bailing, in case the
      // challenge overlay sits on top of still-rendered comments.
      try { harvest(); } catch (e) { /* swallow */ }
      resolve({
        records: [...acc.values()],
        ticks,
        stagnant,
        scroll_height_final: document.documentElement.scrollHeight,
        ticks_log: ticksLog,
        early_stop_reason: challenge,
      });
      return;
    }

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
          early_stop_reason: null,
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


def scrape(
    out_path: Optional[str],
    max_scrolls: int,
    debug_dir: Optional[str] = None,
) -> dict:
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

    2026-05-26: added optional debug_dir. When set, every fire writes
    a forensic bundle (screenshots, html, cookies, console+nav+network
    jsonl, error trace) and tar.gz's it. See _DebugRecorder docstring
    for the full file layout. Disabled when debug_dir is None.
    """
    from playwright.sync_api import sync_playwright

    _acquire_browser_lock()

    dbg = _DebugRecorder(debug_dir)

    # Helper so every return path can finalize the bundle and surface the
    # tarball location. The tarball path goes into the result dict (so
    # main() can echo it on stdout) AND to stderr as a single
    # `[scrape_linkedin] debug_bundle=<path>` marker (so the shell can
    # grep for it without re-parsing JSON).
    def _finalize_and_return(result: dict) -> dict:
        tarball = dbg.finalize(result)
        if tarball:
            result["debug_bundle"] = tarball
            print(
                f"[scrape_linkedin] debug_bundle={tarball}",
                file=sys.stderr,
                flush=True,
            )
        return result

    with sync_playwright() as p:
        try:
            context, owns_context = _connect_to_running_or_launch(p)
        except Exception as e:
            return _finalize_and_return({
                "ok": False,
                "error": "profile_locked",
                "detail": str(e),
            })

        # Mode hint: caller knows from stderr whether we cdp-attached or
        # cold-launched. The bundle gets the same info as a top-level file
        # so it's grep-able from a tarball without unpacking everything.
        dbg.note_owns_context(owns_context)
        dbg.capture_browser_version(context)

        page = None
        try:
            page = context.new_page()

            # Subscribe to page events BEFORE goto so the navigation
            # chain (homepage -> /authwall -> /login) is captured in
            # navigation.jsonl. After goto is too late: we'd miss the
            # opening redirect that is the smoking gun for
            # session_invalid.
            dbg.attach_page_listeners(page)
            dbg.snapshot(page, "01_pre_goto")

            try:
                page.goto(
                    COMMENTS_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                dbg.failure(page, "navigation_failed", str(e))
                return _finalize_and_return({
                    "ok": False,
                    "error": "navigation_failed",
                    "detail": str(e),
                })

            # Settle.
            try:
                page.wait_for_selector(
                    "article, main",
                    timeout=10000,
                )
            except Exception:
                pass
            page.wait_for_timeout(2500)

            # Post-goto checkpoint: URL, html, screenshot, cookie jar.
            # Captured BEFORE the auth/captcha gates so we always have a
            # last-known-state dump even when those gates fire.
            dbg.capture_url(page, "02_post_goto")
            dbg.snapshot(page, "02_post_goto")
            dbg.capture_cookies(context, "02_cookies")

            cur_url = page.url
            if _is_login_or_checkpoint(cur_url):
                dbg.failure(page, "session_invalid", cur_url)
                return _finalize_and_return({
                    "ok": False,
                    "error": "session_invalid",
                    "url": cur_url,
                })

            challenge = _looks_like_captcha_or_checkpoint(page)
            if challenge:
                dbg.failure(page, "captcha_or_checkpoint", challenge)
                return _finalize_and_return({
                    "ok": False,
                    "error": "captcha_or_checkpoint",
                    "url": cur_url,
                    "detail": challenge,
                })

            if not _comments_tab_present(page):
                # Page loaded but isn't the comments tab. Could be
                # rate-limit landing page, A/B-tested redesign that
                # broke our selectors, or a soft 404.
                try:
                    title = page.title() or ""
                except Exception:
                    title = ""
                dbg.failure(page, "wrong_page", f"title={title}")
                return _finalize_and_return({
                    "ok": False,
                    "error": "wrong_page",
                    "url": cur_url,
                    "title": title,
                })

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
                dbg.failure(page, "evaluate_failed", str(e))
                return _finalize_and_return({
                    "ok": False,
                    "error": "evaluate_failed",
                    "detail": str(e),
                })

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

            return _finalize_and_return(out)
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
                    "stats-linkedin.sh (2026-05-11: the standalone "
                    "stats-linkedin-comments.sh was retired after the "
                    "replies-table rows were migrated into posts). Set "
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
    ap.add_argument("--debug-dir", default=None,
                    help="Optional directory to write a forensic bundle "
                         "(screenshots, html, cookies, console+nav+network "
                         "jsonl, error trace). Auto-tar.gz'd at exit; the "
                         "path is echoed to stderr as "
                         "`[scrape_linkedin] debug_bundle=<path>` for the "
                         "shell caller to surface. Disabled when omitted.")
    args = ap.parse_args()

    try:
        result = scrape(args.out, args.max_scrolls, debug_dir=args.debug_dir)
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
