"""Shared harness-tab lifecycle: ONE park-on-exit implementation for every
platform browser lib (twitter_browser.py, reddit_browser.py, future).

Born 2026-07-17 from the user's unification directive: the parking/revive
discipline lived only in twitter_browser.py, so the reddit harness idled on
live reddit pages in a visible window and kept its own failure modes. Per
feedback_minimize_code_footprint there must be exactly one implementation.

Design constraints inherited from the twitter parker (see
insights_chrome150_renderer_crash_wedge_2026_07_14):
  - Raw CDP over HTTP+websocket with hard 3s timeouts, NEVER Playwright:
    at atexit the Playwright loop may be gone, and against a wedged browser
    this must fail fast, not hang the exit.
  - suppress_origin: Chrome 111+ rejects ws clients whose Origin header is
    not in --remote-allow-origins.
  - Park target is the platform's own /robots.txt, NOT about:blank: a static
    ~100-byte document with no SPA to leak or crash, whose URL still matches
    every tab-reuse heuristic (bh list_tabs hides "about:" as internal, and
    an about:blank park made scans mint new tabs — the proven focus-steal
    trigger Target.createTarget).
  - Skip tabs already parked; never raise; S4L_NO_TAB_PARK=1 escape hatch.
"""
import atexit
import json
import os
import sys
import urllib.request

# Loopback CDP must never route through a proxy (macOS system proxy settings
# leak into urllib's default opener; a box-wide forwarder 403s 127.0.0.1).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_registered = set()


def _targets(cdp_base: str):
    with _OPENER.open(cdp_base.rstrip("/") + "/json/list", timeout=3) as r:
        return json.loads(r.read())


def _navigate_ws(ws_url: str, url: str) -> None:
    import websocket

    ws = websocket.create_connection(ws_url, timeout=3, suppress_origin=True)
    try:
        ws.send(json.dumps({
            "id": 1, "method": "Page.navigate", "params": {"url": url},
        }))
        ws.recv()
    finally:
        ws.close()


def park_tabs(cdp_base: str, host_markers, park_url: str, label: str) -> None:
    """Navigate every page tab whose URL matches host_markers to park_url.
    Best-effort; a wedged browser just fails the 3s connects quietly."""
    try:
        for t in _targets(cdp_base):
            u = t.get("url", "")
            if t.get("type") != "page":
                continue
            if not any(h in u for h in host_markers):
                continue
            if u.split("?", 1)[0].rstrip("/").endswith("/robots.txt"):
                continue  # already parked
            if not t.get("webSocketDebuggerUrl"):
                continue
            try:
                _navigate_ws(t["webSocketDebuggerUrl"], park_url)
                print(
                    f"[{label}] parked tab on {park_url} (was {u[:80]})",
                    file=sys.stderr,
                )
            except Exception:
                continue
    except Exception:
        pass


def background_new_page(browser, context, url: str = "about:blank", timeout_ms: int = 10_000):
    """Create a Playwright page WITHOUT raising the Chrome window.

    Playwright's context.new_page() maps to a FOREGROUND Target.createTarget,
    which activates Chrome on macOS and steals app focus (the July 2026
    focus-steal class; same call the bh helpers and the harness daemon were
    already fixed to avoid). Playwright's public API has no background
    option, so this creates the target via a browser-level CDP session with
    background:true and returns the Page that `context` adopts for it.

    ONE shared implementation for every attach path that previously called
    context.new_page() on the harness (reddit_browser, twitter_browser,
    reddit_browser_fetch). Falls back to context.new_page() if the CDP path
    fails: a rare focus blip beats a dead pipeline.
    """
    try:
        cdp = browser.new_browser_cdp_session()
        try:
            with context.expect_page(timeout=timeout_ms) as pg_info:
                cdp.send("Target.createTarget", {"url": url, "background": True})
            # Log SUCCESS too (2026-08-05): the residual focus-steal hunt hit
            # same-second orphan blank creates that no log claimed. Success
            # was silent by design, which made "our background create fired
            # and coincided" indistinguishable from "an unknown foreground
            # creator". One line per create closes that ambiguity.
            _activity_log("bg_new_page", f"url={url[:60]}")
            return pg_info.value
        finally:
            try:
                cdp.detach()
            except Exception:
                pass
    except Exception as e:
        # LOUD fallback: this is the one remaining way a harness tab can be
        # created in the foreground (= macOS focus steal). Log to stderr AND
        # the universal browser-activity.log, because several lanes' stderr is
        # captured into MCP results and never reaches disk (2026-08-04: an
        # unattributed focus-pop hit exactly this observability hole).
        msg = (f"[background_new_page] CDP background create failed "
               f"({type(e).__name__}: {str(e)[:120]}); falling back to "
               f"FOREGROUND new_page (focus steal likely)")
        print(msg, file=sys.stderr)
        _activity_log("fg_fallback", f"detail={type(e).__name__}")
        return context.new_page()


def _activity_log(action: str, detail: str = "") -> None:
    """One line into the universal browser-activity.log (same file the
    Python-CDP attach logging uses). Best-effort; never raises."""
    try:
        import time as _t

        _p = os.path.expanduser("~/.claude/browser-profiles/browser-activity.log")
        with open(_p, "a") as _f:
            _f.write(f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] pycdp "
                     f"script=browser_lifecycle.py action={action} "
                     f"pid={os.getpid()} argv0={os.path.basename(sys.argv[0] or '?')} "
                     f"{detail}\n")
    except Exception:
        pass


def register_park_on_exit(cdp_base: str, host_markers, park_url: str, label: str) -> None:
    """Arm park_tabs to run at process exit, once per (endpoint, park_url).
    Call from a platform lib's get_browser_and_page so only processes that
    actually used the browser park it."""
    if os.environ.get("S4L_NO_TAB_PARK"):
        return
    key = (cdp_base, park_url)
    if key in _registered:
        return
    _registered.add(key)
    atexit.register(park_tabs, cdp_base, tuple(host_markers), park_url, label)
