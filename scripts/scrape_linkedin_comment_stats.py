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
import signal
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
        02_storage.json        localStorage + sessionStorage dump
                               (LinkedIn stores some auth state outside
                               cookies; presence of lidc / lang in
                               localStorage IS a diagnostic signal)
        99_failure.png         screenshot at error-return path
        99_failure.html        outerHTML at error-return path
        99_failure.txt         error/detail + Python traceback
        console.jsonl          page console messages + uncaught pageerrors
        navigation.jsonl       framenavigated events (ALL frames; the
                               authwall redirect chain is the data here)
        network.jsonl          response events for *.linkedin.com requests
                               (status, url, content-type; body truncated
                               to 2KB to keep bundle tractable)
        requests.jsonl         request events for *.linkedin.com (URL,
                               method, resource_type, headers, post_data
                               truncated to 2KB). Catches POSTs / beacons
                               that on_response alone can't surface.
        requests_failed.jsonl  network-level failures (DNS, abort,
                               connection-refused). Empty on clean fires.
        harvest_js_source.js   the exact JS template that ran inside
                               page.evaluate. Captured per-fire so a
                               future failure can be diffed against the
                               version of HARVEST_JS that produced it.
        trace.zip              Playwright trace (snapshots + screenshots
                               + network + console + sources). Open with
                               `npx playwright show-trace <path>`.
                               Best single forensic artifact when present.
        meta.json              start/end timestamps + scrape summary +
                               per-phase timings (cdp_attach_ms, goto_ms,
                               settle_ms, evaluate_ms, …) + viewport +
                               saw_429 events

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
        self._fh_req = None
        self._fh_reqfail = None
        # Tracing state. Stored so finalize() can stop tracing before it
        # tars the bundle (the trace.zip must exist on disk when tar
        # runs). _context kept as a weakref-style handle; if Playwright
        # tears down the context before we stop tracing, the stop call
        # will raise and we swallow.
        self._tracing_started: bool = False
        self._context = None
        # Phase timings filled in by set_timing(). Surfaced in meta.json.
        self.timings: dict = {}
        # Soft abort signal raised by on_response when 429 count crosses
        # ABORT_429_THRESHOLD. Polled by scrape() after page.evaluate()
        # returns so we don't burn through the post-throttle window with
        # follow-up scrolls. JS-side scroll loop runs in a separate exec
        # context and can't observe this; the bailout is post-loop.
        self._abort_reason: Optional[str] = None
        self._saw_429_count: int = 0
        # Killswitch state (added 2026-05-27). Set by on_response /
        # on_framenav when a hard signal fires. Engaged exactly once per
        # scrape() invocation via _engage_killswitch_if_signal(); the
        # killswitch file itself is idempotent (first signal wins) so a
        # double-fire here is harmless but wasteful.
        self._kill_signal: Optional[str] = None
        self._kill_detail: str = ""
        self._killswitch_engaged: bool = False
        # Pagination canary: count voyagerFeedDashProfileUpdates calls.
        # Healthy runs see 5+; throttled runs see <=1 (initial paint only).
        self._voyager_paginate_calls: int = 0
        # Wall-clock start for the throttle window. Set in __init__ so
        # _engage_killswitch_if_signal can compute scrape runtime even
        # if it fires from a late error-return path.
        self._scrape_started_at: float = time.time()
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
        for attr in ("_fh_console", "_fh_nav", "_fh_net",
                     "_fh_req", "_fh_reqfail"):
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
                is_main = frame == page.main_frame
                rec = {
                    "ts": _ts_ms(),
                    "url": frame.url,
                    "name": frame.name,
                    "is_main": is_main,
                }
                # Main-frame redirect canary. Any of these means the
                # session is gone (or going) and we MUST stop. Detect
                # here, before the auth gate at line ~1230, so the
                # killswitch fires even on async redirects that happen
                # after page.goto returned cleanly.
                if is_main and self._kill_signal is None:
                    u = (frame.url or "").lower()
                    if "/authwall" in u:
                        self._kill_signal = "authwall_redirect"
                        self._kill_detail = f"main-frame -> {frame.url}"
                    elif "/checkpoint/" in u or "/checkpoint?" in u:
                        self._kill_signal = "checkpoint_redirect"
                        self._kill_detail = f"main-frame -> {frame.url}"
                    elif (
                        "/uas/login" in u
                        or u.endswith("/login")
                        or "/login?" in u
                    ):
                        # Exclude the same-origin /login redirect we
                        # cause ourselves on a SESSION_INVALID. Only
                        # fire for the LinkedIn-initiated redirect.
                        if "linkedin.com" in u:
                            self._kill_signal = "login_redirect"
                            self._kill_detail = f"main-frame -> {frame.url}"
                    if self._kill_signal:
                        print(
                            f"[scrape_linkedin] KILL_SIGNAL="
                            f"{self._kill_signal} url={frame.url[:200]}",
                            file=sys.stderr,
                            flush=True,
                        )
            except Exception as e:
                rec = {"ts": _ts_ms(), "err": repr(e)}
            self._append_jsonl("_fh_nav", "navigation.jsonl", rec)

        def on_response(response):
            # LinkedIn-only: keeps bundle <1MB on a typical run.
            try:
                url = response.url
                if "linkedin.com" not in url:
                    return
                # HTTP 999: LinkedIn's "you're flagged" canary. Hard
                # signal: any 999 from linkedin.com means the session
                # is being throttled at the edge. 2026-05-27 forensic:
                # GET /in/me/recent-activity/comments/ returned 999,
                # then 302'd to /authwall?trk=bf. Trip the killswitch
                # immediately, no threshold needed.
                if response.status == 999 and self._kill_signal is None:
                    self._kill_signal = "http_999"
                    self._kill_detail = (
                        f"{response.request.method} {url[:300]} -> 999"
                    )
                    print(
                        f"[scrape_linkedin] KILL_SIGNAL=http_999 "
                        f"url={url[:200]}",
                        file=sys.stderr,
                        flush=True,
                    )
                # Voyager pagination canary. Count calls to the recent-
                # activity-comments graphql endpoint. Post-scroll, if
                # this count is <THROTTLE_PAGINATION_MIN_CALLS, we are
                # being silently throttled.
                if VOYAGER_PAGINATION_QUERYID in url:
                    self._voyager_paginate_calls += 1
                # li_at cookie clearing. LinkedIn signs us out by
                # sending Set-Cookie: li_at=; Max-Age=0 (or similar)
                # in the authwall response. Catch that here before
                # the next request even fires so the killswitch
                # engages on the FIRST cleared response, not after
                # the redirect chain completes.
                try:
                    sc = response.headers.get("set-cookie") or ""
                    if sc:
                        sc_low = sc.lower()
                        if "li_at=" in sc_low and (
                            "max-age=0" in sc_low
                            or "li_at=;" in sc_low
                            or 'li_at="";' in sc_low
                            or "expires=thu, 01 jan 1970" in sc_low
                        ):
                            if self._kill_signal is None:
                                self._kill_signal = "li_at_cleared"
                                self._kill_detail = (
                                    f"Set-Cookie cleared li_at on "
                                    f"{url[:200]}"
                                )
                                print(
                                    f"[scrape_linkedin] KILL_SIGNAL="
                                    f"li_at_cleared url={url[:200]}",
                                    file=sys.stderr,
                                    flush=True,
                                )
                except Exception:
                    pass
                # Rate-limit canary. LinkedIn rarely returns a bare 429 —
                # it usually redirects to /authwall or injects a captcha
                # overlay (both caught by the in-JS detectChallengeInDom
                # gate). But when a raw 429 does fire, surface it as a
                # grep-able stderr marker so the orchestrator log shows
                # the canary even when the run continues. Also stamp
                # meta.json so the in-bundle summary records it.
                if response.status == 429:
                    self._saw_429_count += 1
                    print(
                        f"[scrape_linkedin] saw_429 "
                        f"count={self._saw_429_count} url={url[:200]}",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        self.meta.setdefault("saw_429", []).append({
                            "ts": _ts_ms(), "url": url[:200],
                        })
                    except Exception:
                        pass
                    if (self._saw_429_count >= ABORT_429_THRESHOLD
                            and self._abort_reason is None):
                        self._abort_reason = (
                            f"saw_429_count={self._saw_429_count}"
                        )
                        print(
                            f"[scrape_linkedin] ABORT signal raised "
                            f"reason={self._abort_reason}",
                            file=sys.stderr,
                            flush=True,
                        )
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

        def on_request(req):
            # LinkedIn-only filter mirrors on_response. Catches POSTs +
            # beacons that on_response can't surface on its own (a
            # silently-dropped POST shows up here, not there).
            try:
                url = req.url
                if "linkedin.com" not in url:
                    return
                post_data = None
                try:
                    pd = req.post_data
                    if pd:
                        post_data = pd[:2048]
                except Exception:
                    pass
                rec = {
                    "ts": _ts_ms(),
                    "method": req.method,
                    "url": url,
                    "type": req.resource_type,
                    "headers": dict(list(req.headers.items())[:30]),
                    "post_data": post_data,
                }
            except Exception as e:
                rec = {"ts": _ts_ms(), "err": repr(e)}
            self._append_jsonl("_fh_req", "requests.jsonl", rec)

        def on_request_failed(req):
            # Network-level failures (DNS, abort, connection-refused).
            # Empty on clean fires; the first appearance is a strong
            # signal that LinkedIn cut us off below the HTTP layer.
            try:
                rec = {
                    "ts": _ts_ms(),
                    "method": req.method,
                    "url": req.url,
                    "type": req.resource_type,
                    "failure": getattr(req, "failure", None),
                }
            except Exception as e:
                rec = {"ts": _ts_ms(), "err": repr(e)}
            self._append_jsonl(
                "_fh_reqfail", "requests_failed.jsonl", rec
            )

        try:
            page.on("console", on_console)
            page.on("pageerror", on_pageerror)
            page.on("framenavigated", on_framenav)
            page.on("response", on_response)
            page.on("request", on_request)
            page.on("requestfailed", on_request_failed)
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

    def start_tracing(self, context) -> None:
        """Begin Playwright tracing on the attached context.

        Tracing produces a single .zip with DOM snapshots, screenshots,
        network, console, and source-stack-traces at every Playwright
        action. Open with `npx playwright show-trace <path>` to step
        through the scrape interactively. Best single forensic artifact
        we capture.

        CDP-attached contexts CAN trace (Playwright supports it for
        connect_over_cdp) but the underlying browser must be Playwright-
        compatible — Chrome 148 is. Wrapped in try/except so a tracing
        failure never derails the actual scrape.
        """
        if not self.enabled or context is None:
            return
        self._context = context
        try:
            context.tracing.start(
                screenshots=True,
                snapshots=True,
                sources=True,
                title="stats-linkedin-scrape",
            )
            self._tracing_started = True
        except Exception as e:
            print(
                f"[scrape_linkedin] WARN: tracing.start failed: {e!r}",
                file=sys.stderr,
                flush=True,
            )
            self._tracing_started = False

    def stop_tracing(self) -> None:
        """Stop tracing and write trace.zip into the bundle dir.

        Called from finalize() BEFORE the tarball is created so the
        trace.zip ends up inside the .tar.gz alongside the other
        artifacts. Idempotent: safe to call when tracing never started.
        """
        if not self.enabled or not self._tracing_started:
            return
        if self._context is None:
            return
        out = self._path("trace.zip")
        if not out:
            return
        try:
            self._context.tracing.stop(path=out)
        except Exception as e:
            print(
                f"[scrape_linkedin] WARN: tracing.stop failed: {e!r}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            # One-shot. Don't try to stop again from a later code path.
            self._tracing_started = False

    def capture_storage(self, page) -> None:
        """Dump localStorage + sessionStorage to 02_storage.json.

        LinkedIn keeps some auth + UX state outside cookies (lidc,
        recently-viewed flags, A/B test buckets). Presence / absence of
        specific keys is occasionally the only signal that distinguishes
        "logged-in but throttled" from "session forced to bg state".
        Quotas can hold ~5MB per origin but real LinkedIn storage is
        usually <100KB so no truncation needed.
        """
        if not self.enabled or page is None:
            return
        try:
            data = page.evaluate(
                """() => {
                  const dump = (s) => {
                    const o = {};
                    for (let i = 0; i < s.length; i++) {
                      const k = s.key(i);
                      try { o[k] = s.getItem(k); }
                      catch (e) { o[k] = '<read_failed:' + e + '>'; }
                    }
                    return o;
                  };
                  return {
                    local: dump(window.localStorage),
                    session: dump(window.sessionStorage),
                  };
                }"""
            ) or {}
        except Exception as e:
            self._write_text(
                "02_storage.err.txt",
                f"storage_read_failed: {e!r}\nts={_ts_ms()}\n",
            )
            return
        try:
            self._write_text(
                "02_storage.json",
                json.dumps(data, indent=2, default=str),
            )
        except Exception as e:
            self._write_text(
                "02_storage.err.txt",
                f"storage_serialize_failed: {e!r}\nts={_ts_ms()}\n",
            )

    def capture_harvest_js(self, js_source: str) -> None:
        """Snapshot the JS template that ran inside page.evaluate.

        Captured per-fire so a future failure DOM can be diffed against
        the exact version of HARVEST_JS that produced it. Keeps the
        bundle self-describing: you can replay the scrape against the
        captured 02_post_goto.html locally without git-checking-out the
        scraper revision that ran.
        """
        if not self.enabled:
            return
        self._write_text("harvest_js_source.js", js_source or "")

    def capture_viewport(self, page) -> None:
        """Record viewport size + scroll position into self.meta.

        Surfaced as meta.json.viewport. Catches the case where Chrome
        booted with an unexpected window size (mobile-emulation flag
        leaked, --window-size override forgotten) that would cause our
        scroll math to miss content. Best-effort; never raises.
        """
        if not self.enabled or page is None:
            return
        view = {}
        try:
            vp = page.viewport_size or {}
            view["width"] = vp.get("width")
            view["height"] = vp.get("height")
        except Exception:
            pass
        try:
            scroll = page.evaluate(
                """() => ({
                  scroll_y: window.scrollY,
                  scroll_x: window.scrollX,
                  inner_w: window.innerWidth,
                  inner_h: window.innerHeight,
                  document_h: document.documentElement.scrollHeight,
                  device_pixel_ratio: window.devicePixelRatio,
                  user_agent: navigator.userAgent,
                })"""
            ) or {}
            view.update(scroll)
        except Exception as e:
            view["err"] = repr(e)
        self.meta["viewport"] = view

    def set_timing(self, name: str, ms: int) -> None:
        """Record a per-phase elapsed time in milliseconds.

        Called from scrape() around each major step (cdp_attach, goto,
        settle, evaluate, ...). Aggregated under meta.json.timings on
        finalize. Lets a future "scrape took 90s, why?" investigation
        skip the timestamp arithmetic.
        """
        if not self.enabled:
            return
        try:
            self.timings[name] = int(ms)
        except Exception:
            pass

    def failure(self, page, error: str, detail: str = "") -> None:
        """Capture failure-mode artifacts: screenshot, html, error text.

        Also routes the failure to the killswitch when the error code
        is unambiguous (session_invalid, captcha_or_checkpoint), or when
        a listener earlier set self._kill_signal from a network signal."""
        # Killswitch engagement runs even when self.enabled is False; the
        # debug recorder being disabled has no bearing on whether we
        # should halt the pipelines.
        try:
            self.engage_killswitch_for_failure(error, detail, page)
        except Exception as _e:
            print(
                f"[scrape_linkedin] WARN: killswitch engage in failure() "
                f"raised: {_e!r}",
                file=sys.stderr,
                flush=True,
            )
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
            f"kill_signal={self._kill_signal}\n"
            f"kill_detail={self._kill_detail}\n"
            f"voyager_paginate_calls={self._voyager_paginate_calls}\n"
            f"\n--- python traceback ---\n"
            f"{traceback.format_exc()}"
        )
        self._write_text("99_failure.txt", body)

    # --- killswitch glue --------------------------------------------------

    # Map error codes coming out of scrape() to killswitch signal names.
    # Listed errors trip the killswitch unconditionally; any error NOT
    # listed here trips the killswitch ONLY if a listener already set
    # self._kill_signal (network-level signals like http_999, authwall
    # redirect, li_at_cleared, voyager-throttle-detected).
    _FAILURE_TO_SIGNAL = {
        "session_invalid": "session_invalid_marker",
        "captcha_or_checkpoint": "captcha_detected",
    }

    def maybe_detect_throttle(self, with_impressions: int = 0) -> None:
        """Post-evaluate throttle detection.

        Called after page.evaluate() returns. If the scroll loop ran for
        at least THROTTLE_MIN_RUNTIME_SEC and we saw fewer than
        THROTTLE_PAGINATION_MIN_CALLS voyagerFeedDashProfileUpdates calls,
        LinkedIn is silently dropping our pagination XHRs and the session
        is being shadow-throttled. Trip the killswitch signal so the
        next failure() call (or the post-evaluate engagement below)
        engages the killswitch.

        HEALTHY-BUNDLE GUARD (2026-06-04): a low pagination count is only
        evidence of throttling when the scrape ALSO came back thin/empty.
        An account with few recent comments legitimately needs just one
        voyager page: all records fit on page 1, so paginate_calls==1 even
        though nothing was dropped. If we harvested >=1 record carrying
        impressions AND saw zero 429s, pagination demonstrably worked and
        the session is healthy; never trip the killswitch on that. This
        fixes the false positive that latched the killswitch on a 5-record
        bundle (with_impressions=5, saw_429=0) and froze every LinkedIn
        pipeline for ~8h on 2026-06-04."""
        if self._kill_signal is not None:
            return
        if with_impressions > 0 and self._saw_429_count == 0:
            return
        runtime = time.time() - self._scrape_started_at
        if runtime < THROTTLE_MIN_RUNTIME_SEC:
            return
        if self._voyager_paginate_calls < THROTTLE_PAGINATION_MIN_CALLS:
            self._kill_signal = "throttle_no_pagination"
            self._kill_detail = (
                f"voyager_paginate_calls={self._voyager_paginate_calls} "
                f"(min={THROTTLE_PAGINATION_MIN_CALLS}) "
                f"runtime_sec={int(runtime)}"
            )
            print(
                f"[scrape_linkedin] KILL_SIGNAL=throttle_no_pagination "
                f"{self._kill_detail}",
                file=sys.stderr,
                flush=True,
            )

    def engage_killswitch_for_failure(
        self, error: str, detail: str, page,
    ) -> None:
        """Engage the killswitch if this failure code maps to a signal,
        OR if a listener already set self._kill_signal from a network
        observation. Idempotent within the process via
        self._killswitch_engaged; the killswitch file itself is also
        idempotent so a duplicate call is a no-op."""
        if self._killswitch_engaged:
            return
        signal_name = self._kill_signal
        signal_detail = self._kill_detail
        if not signal_name:
            signal_name = self._FAILURE_TO_SIGNAL.get(error)
            if signal_name:
                signal_detail = f"error={error} detail={detail}"
        if not signal_name:
            return
        try:
            url = page.url if page is not None else ""
        except Exception:
            url = ""
        run_log_path = os.environ.get("SAPS_RUN_LOG_PATH", "")
        try:
            linkedin_killswitch.engage(
                signal=signal_name,
                detail=signal_detail or f"error={error}",
                run_log_path=run_log_path,
                extra={
                    "url": url,
                    "scrape_error": error,
                    "scrape_detail": detail,
                    "voyager_paginate_calls": self._voyager_paginate_calls,
                    "saw_429_count": self._saw_429_count,
                    "debug_dir": self.dir,
                },
            )
            self._killswitch_engaged = True
            print(
                f"[scrape_linkedin] LINKEDIN_KILLSWITCH_ENGAGED "
                f"signal={signal_name} error={error}",
                file=sys.stderr,
                flush=True,
            )
        except Exception as e:
            print(
                f"[scrape_linkedin] WARN: linkedin_killswitch.engage "
                f"raised: {e!r}",
                file=sys.stderr,
                flush=True,
            )

    def finalize(self, result: dict) -> Optional[str]:
        """Write meta.json, close jsonl handles, tar.gz the dir.

        Returns absolute path to the .tar.gz on success, None on failure
        or when disabled. The shell caller surfaces this path in its log
        and (on session_invalid) promotes it to a permanent archive.
        """
        if not self.enabled or not self.dir:
            return None
        # Stop tracing FIRST so trace.zip lands in the dir before tarring.
        # Idempotent + try/except internally so a tracing failure can't
        # block meta.json + the tarball.
        self.stop_tracing()
        self.meta["started_at"] = self.started_at
        self.meta["finished_at"] = _ts_ms()
        self.meta["pid"] = os.getpid()
        self.meta["ok"] = bool(result.get("ok"))
        self.meta["error"] = result.get("error")
        self.meta["records"] = result.get("record_count")
        self.meta["with_impressions"] = result.get("with_impressions")
        self.meta["with_reactions"] = result.get("with_reactions")
        if self.timings:
            self.meta["timings"] = self.timings
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
DEFAULT_MAX_SCROLLS = 80
SCROLL_PAUSE_MIN_MS = 2500
SCROLL_PAUSE_MAX_MS = 6500
SCROLL_DY_MIN = 600
SCROLL_DY_MAX = 1100
HARVEST_SETTLE_MS = 1500
# Number of 429 responses (LinkedIn or sub-resource) before we raise the
# soft abort flag inside _DebugRecorder. Once tripped, scrape() bails out
# after the current page.evaluate() returns, preserving whatever records
# the JS loop already accumulated. Three is enough to distinguish a real
# throttle from a one-off API hiccup but tight enough to stop the bleed
# before LinkedIn escalates the session to /checkpoint.
ABORT_429_THRESHOLD = 3

# Killswitch thresholds (added 2026-05-27 after the behavioral fingerprint
# session revocation). Forensic data from the 2026-05-27 run:
#   healthy:    5-17 voyagerFeedDashProfileUpdates pagination calls
#   throttled:  1 call (initial paint only; pagination XHRs silently dropped)
#   authwalled: 0 calls
# So "post-scroll loop, fewer than 2 voyager calls" is a reliable throttle
# canary. We only fire it after the loop has run for THROTTLE_MIN_RUNTIME_SEC
# (60s) so a fast-error fire doesn't spuriously trip it.
THROTTLE_PAGINATION_MIN_CALLS = 2
THROTTLE_MIN_RUNTIME_SEC = 60
# Voyager queryId we use as the pagination canary. LinkedIn occasionally
# renames these (e.g. when they ship a new feed surface), so this constant
# is the single point of update. If they rename it, the canary goes silent
# and throttle detection becomes too tight; watch the trail log for a
# spike in throttle_no_pagination engagements on healthy-looking bundles.
VOYAGER_PAGINATION_QUERYID = "voyagerFeedDashProfileUpdates"

# Killswitch helper is a sibling module; import is best-effort so an
# import error here can NEVER block a scrape from running. If the import
# fails we fall back to a no-op shim (engage() does nothing).
try:
    import linkedin_killswitch  # noqa: E402
except Exception as _e_killswitch:
    class _KillswitchShim:
        @staticmethod
        def engage(*_a, **_k):
            return None
        @staticmethod
        def is_active():
            return False
    linkedin_killswitch = _KillswitchShim()  # type: ignore
    print(
        f"[scrape_linkedin] WARN: linkedin_killswitch import failed: "
        f"{_e_killswitch!r}; killswitch engage will no-op",
        file=sys.stderr,
        flush=True,
    )


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

  // Bug A fix (2026-05-27): scope the stagnation check to the bottom
  // edge of the LAST comment article rather than document.scrollHeight.
  // Diagnostic console.log on the prior fire proved that sidebar / page
  // chrome mutations push documentElement.scrollHeight up (dsh=608, 23)
  // even when added=0, resetting `stagnant` to 0 and keeping the loop
  // alive against an exhausted feed. Measuring the last comment's
  // absolute bottom is immune to that.
  function lastCommentBottomPx() {
    let lastBottom = 0;
    const arts = document.querySelectorAll('article');
    for (const art of arts) {
      if (!art.querySelector(
        '[data-urn^="urn:li:comment:"], [data-id^="urn:li:comment:"]'
      )) continue;
      const r = art.getBoundingClientRect();
      const b = r.bottom + window.scrollY;
      if (b > lastBottom) lastBottom = b;
    }
    return lastBottom;
  }

  let ticks = 0;
  let stagnant = 0;  // consecutive ticks with no new comments
  let lastScrollHeight = document.documentElement.scrollHeight;
  let lastCommentBottom = lastCommentBottomPx();
  // Bug B fix (2026-05-27): self-imposed deadline so the JS loop bails
  // cleanly BEFORE Python's gtimeout fires SIGKILL. CDP does not cancel
  // executing JS when the client disconnects, so prior runs left tabs
  // scrolling indefinitely after the Python parent died. Default keeps
  // the loop inside its budget; Python passes `opts.deadline_ms`.
  const startTime = Date.now();

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

    // Bug B fix: self-imposed deadline. If Python's gtimeout would fire
    // before we naturally bail, stop NOW with whatever we've harvested
    // and emit `early_stop_reason='deadline'` so the writer still gets
    // partial records.
    if (opts.deadline_ms && (Date.now() - startTime) >= opts.deadline_ms) {
      try { harvest(); } catch (e) { /* swallow */ }
      resolve({
        records: [...acc.values()],
        ticks,
        stagnant,
        scroll_height_final: document.documentElement.scrollHeight,
        ticks_log: ticksLog,
        early_stop_reason: 'deadline_ms_reached',
      });
      return;
    }

    const added = harvest();
    const sh = document.documentElement.scrollHeight;
    const cb = lastCommentBottomPx();
    ticksLog.push({tick: ticks, added, total: acc.size,
                   scroll_height: sh, comment_bottom: cb});

    // Early-stop if the LAST comment's bottom position hasn't moved AND
    // no new comments were added. The original guard (`sh === last`)
    // false-negatived on sidebar/page-chrome mutations (Bug A,
    // confirmed by per-tick diagnostic 2026-05-27).
    if (added === 0 && cb === lastCommentBottom) {
      stagnant++;
    } else {
      stagnant = 0;
    }
    // Per-tick diagnostic. `dsh` shows whole-document drift (sidebar);
    // `dcb` shows comment-list drift (what stagnant now keys on).
    console.log('[scrape_tick] tick=' + ticks
      + ' added=' + added
      + ' acc=' + acc.size
      + ' sh=' + sh
      + ' dsh=' + (sh - lastScrollHeight)
      + ' cb=' + cb
      + ' dcb=' + (cb - lastCommentBottom)
      + ' stagnant=' + stagnant);
    lastScrollHeight = sh;
    lastCommentBottom = cb;

    const dy = opts.dy_min + Math.random() * (opts.dy_max - opts.dy_min);
    window.scrollBy(0, dy);
    ticks++;

    const wait = opts.pause_min_ms
               + Math.random() * (opts.pause_max_ms - opts.pause_min_ms);

    if (ticks < opts.max_scrolls && stagnant < 8) {
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
    existing harness Chrome's BrowserContext, so cookies/fingerprint
    match perfectly and no second Chrome process is ever spawned.

    2026-05-31: harness-only. The helper's Lane 2 fallback (legacy
    DevToolsActivePort attach to the linkedin-agent profile) and the
    cold-launch launch_persistent_context path were REMOVED to kill the
    "two LinkedIn browsers in parallel" bug. _connect_to_running_or_launch
    now attaches ONLY to the harness Chrome (port 9556 via
    LINKEDIN_CDP_URL) and raises loudly if it is unreachable. There is no
    longer any cold-MCP fallback.

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
        _t_attach = time.time()
        try:
            context, owns_context = _connect_to_running_or_launch(p)
        except Exception as e:
            return _finalize_and_return({
                "ok": False,
                "error": "profile_locked",
                "detail": str(e),
            })
        dbg.set_timing("cdp_attach_ms", int((time.time() - _t_attach) * 1000))

        # Mode hint: caller knows from stderr whether we cdp-attached or
        # cold-launched. The bundle gets the same info as a top-level file
        # so it's grep-able from a tarball without unpacking everything.
        dbg.note_owns_context(owns_context)
        dbg.capture_browser_version(context)
        dbg.start_tracing(context)

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

            _t_goto = time.time()
            try:
                page.goto(
                    COMMENTS_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                dbg.set_timing(
                    "goto_ms", int((time.time() - _t_goto) * 1000),
                )
                dbg.failure(page, "navigation_failed", str(e))
                return _finalize_and_return({
                    "ok": False,
                    "error": "navigation_failed",
                    "detail": str(e),
                })
            dbg.set_timing("goto_ms", int((time.time() - _t_goto) * 1000))

            # Settle.
            _t_settle = time.time()
            try:
                page.wait_for_selector(
                    "article, main",
                    timeout=10000,
                )
            except Exception:
                pass
            page.wait_for_timeout(2500)
            dbg.set_timing(
                "settle_ms", int((time.time() - _t_settle) * 1000),
            )

            # Post-goto checkpoint: URL, html, screenshot, cookie jar.
            # Captured BEFORE the auth/captcha gates so we always have a
            # last-known-state dump even when those gates fire.
            dbg.capture_url(page, "02_post_goto")
            dbg.snapshot(page, "02_post_goto")
            dbg.capture_cookies(context, "02_cookies")
            dbg.capture_storage(page)
            dbg.capture_viewport(page)

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
            dbg.capture_harvest_js(HARVEST_JS_TEMPLATE)
            _t_eval = time.time()
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
                        # Self-imposed JS deadline (Bug B fix, 2026-05-27).
                        # Picks up SAPS_SCRAPER_DEADLINE_MS if set by the
                        # shell caller; defaults to 10min after the
                        # 2026-05-27 killswitch ship. 35min was the
                        # runaway envelope that gave LinkedIn 25 minutes
                        # of unbroken behavioral fingerprinting before
                        # any external timer fired; 10min is well above
                        # the 56-record healthy-fire cap (~3min) but
                        # below any plausible "we're just slow" tail.
                        "deadline_ms": int(
                            os.environ.get(
                                "SAPS_SCRAPER_DEADLINE_MS", "600000"
                            )
                        ),
                    },
                )
            except Exception as e:
                dbg.set_timing(
                    "evaluate_ms", int((time.time() - _t_eval) * 1000),
                )
                dbg.failure(page, "evaluate_failed", str(e))
                return _finalize_and_return({
                    "ok": False,
                    "error": "evaluate_failed",
                    "detail": str(e),
                })
            dbg.set_timing(
                "evaluate_ms", int((time.time() - _t_eval) * 1000),
            )

            records = result.get("records") or []
            with_imp = sum(
                1 for r in records if r.get("impressions") is not None
            )
            with_rxn = sum(
                1 for r in records if r.get("reactions") is not None
            )
            early_stop_reason = result.get("early_stop_reason")

            # 429 soft-abort: on_response trips dbg._abort_reason once the
            # cumulative 429 count crosses ABORT_429_THRESHOLD. JS scroll
            # loop can't observe it (different exec context), but we catch
            # it post-evaluate and convert into a partial-success bail so
            # the writer still applies whatever the loop did harvest.
            if dbg._abort_reason and not early_stop_reason:
                early_stop_reason = dbg._abort_reason

            # Post-evaluate throttle detection. If the scroll loop ran
            # for >=60s and emitted fewer than 2 voyagerFeedDashProfileUpdates
            # XHRs, LinkedIn is silently dropping our pagination — trip
            # the killswitch signal now. Then engage the killswitch if
            # any signal is set (this covers HTTP 999 / authwall /
            # li_at_cleared / throttle paths where the scroll loop
            # otherwise returned cleanly).
            dbg.maybe_detect_throttle(with_impressions=with_imp)
            if dbg._kill_signal and not early_stop_reason:
                early_stop_reason = f"kill_signal={dbg._kill_signal}"
            if dbg._kill_signal:
                try:
                    dbg.engage_killswitch_for_failure(
                        error="kill_signal_post_evaluate",
                        detail=dbg._kill_detail,
                        page=page,
                    )
                except Exception:
                    pass

            # Hard-fail path: challenge fired before we got ANY records.
            # Treat as captcha_or_checkpoint-equivalent so stats-linkedin.sh
            # can promote the debug bundle to the permanent archive.
            if early_stop_reason and len(records) == 0:
                dbg.failure(
                    page,
                    "early_stop_no_records",
                    early_stop_reason,
                )
                return _finalize_and_return({
                    "ok": False,
                    "error": "early_stop_no_records",
                    "url": cur_url,
                    "early_stop_reason": early_stop_reason,
                })

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
            if early_stop_reason:
                # Partial success: writer still applies the records we did
                # harvest. Surface a grep-able stderr marker so the
                # orchestrator log shows the canary even though rc=0.
                out["partial"] = True
                out["early_stop_reason"] = early_stop_reason
                print(
                    f"[scrape_linkedin] partial_stop "
                    f"reason={early_stop_reason} "
                    f"records={len(records)}",
                    file=sys.stderr,
                    flush=True,
                )

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


def _install_sigterm_trap():
    """Convert SIGTERM/SIGINT into SystemExit so the scrape()'s `finally`
    block runs and closes the page. Bug B fix (2026-05-27): without this,
    gtimeout's SIGTERM kills the Python process but leaves the harvest
    JS executing inside Chrome (CDP does NOT cancel page-side execution
    on client disconnect). The orphan JS keeps scrolling and harvesting
    for minutes, hammering the session and risking a soft ban.

    Pairing this with the JS-side `deadline_ms` self-bail means SIGTERM
    is now a true backstop, not a steady-state cleanup."""
    def _on_term(signum, _frame):
        # 143 = 128 + SIGTERM(15), the conventional exit code for a
        # SIGTERM-killed process. Matches shell `kill -TERM` semantics.
        sys.exit(143 if signum == signal.SIGTERM else 130)
    try:
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
    except (ValueError, OSError):
        # signal.signal() can only run from the main thread; we are
        # invoked as a standalone process so this is the main thread.
        # Swallow defensively in case of future imports-as-module.
        pass


def main():
    _install_sigterm_trap()
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
