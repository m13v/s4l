#!/usr/bin/env python3
"""On-page status overlay for the social-autoposter browser harness.

When the twitter harness drives its dedicated Chrome (port 9555 by default),
the window can look frozen for long stretches while it scans / drafts / posts.
This module injects a small, non-interactive overlay into the harness Chrome so
the user knows (a) they can keep working in other apps and leave the window in
the background (just don't close it), and (b) what the harness is doing right
now, streamed live.

Design constraints, deliberately:
- pointer-events: none on the overlay so it NEVER intercepts the automation's
  own clicks. It is purely cosmetic.
- CSP-safe: the overlay is built with createElement + element.style.<prop> +
  textContent only. No innerHTML-with-style-attributes and no injected <style>
  tag, both of which x.com's CSP can refuse. The "pulse" + "updated Ns ago"
  ticker are driven by a JS setInterval, not CSS @keyframes.
- Survives navigation two ways: (1) Playwright add_init_script registers the
  builder on the browser context so every new document re-creates it, and
  (2) the watch loop re-asserts it via evaluate every couple seconds.

This file is standalone and owns its own integration. It does NOT edit any of
the locked pipeline scripts. Drive it from the CLI:

    python3 harness_overlay.py install                 # show overlay now
    python3 harness_overlay.py status "drafting reply" # update the status line
    python3 harness_overlay.py clear                   # remove the overlay
    python3 harness_overlay.py watch                   # stream the live cycle
                                                        # log into the overlay

`watch` is the always-on mode: it tails the newest skill/logs/twitter-cycle-*.log
and pushes a friendly one-liner into the overlay as each step lands, with a
heartbeat so even idle-looking moments read as alive. If the harness Chrome is
down it sleeps and retries; it never crashes the pipeline.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

# --- self-heal interpreter: Playwright must be importable -------------------
# The pipeline's bare `python3` on this Mac can resolve to a Python without
# Playwright (3.14). Mirror the linkedin-backend.sh resolver: re-exec under the
# first interpreter that actually has playwright before doing any real work.
def _ensure_playwright_interpreter() -> None:
    try:
        import playwright  # noqa: F401
        return
    except Exception:
        pass
    if os.environ.get("_S4L_OVERLAY_REEXEC") == "1":
        return  # already tried; fall through and let the import error surface
    for cand in (
        "/opt/homebrew/bin/python3.11",
        "/usr/bin/python3",
        "/opt/homebrew/bin/python3",
    ):
        if Path(cand).exists() and os.path.realpath(cand) != os.path.realpath(sys.executable):
            env = dict(os.environ, _S4L_OVERLAY_REEXEC="1")
            os.execve(cand, [cand, os.path.abspath(__file__), *sys.argv[1:]], env)


_ensure_playwright_interpreter()

# --- config -----------------------------------------------------------------

CDP_URL = os.environ.get("TWITTER_CDP_URL", "http://127.0.0.1:9555").strip()
LOG_DIR = Path(os.environ.get("S4L_LOG_DIR", str(Path.home() / "social-autoposter" / "skill" / "logs")))
# How stale a cycle log can be (seconds) before we treat the harness as idle.
IDLE_AFTER_SEC = int(os.environ.get("S4L_OVERLAY_IDLE_SEC", "240"))

TITLE = "S4L"
REASSURE = (
    "Working in the background. You can keep using other apps and leave this "
    "window behind \u2014 just don\u2019t close it."
)

# --- the page-side overlay builder ------------------------------------------
# `_BODY` defines window.__s4lPaint(payload): idempotently creates the overlay
# DOM, then updates its text. A lone setInterval drives both the pulse and the
# "updated Ns ago" ticker so the overlay always looks alive between status
# pushes. Built with createElement + element.style.<prop> + textContent only
# (CSP-safe; no <style> tag, no innerHTML-with-style-attrs). pointer-events is
# none so the overlay can never intercept the automation's own clicks.
_BODY = r"""
window.__s4lAnnounce = function(payload){
  // One-time, dismissible-forever launch notice. The reassurance disclaimer
  // lives HERE (a big centered modal with an OK button) instead of eating space
  // in the always-on status overlay. Once OK is clicked we stamp localStorage so
  // it never shows again. Best-effort + CSP-safe (createElement/style/textContent
  // + addEventListener only); never throws into the page.
  try {
    var KEY = "__s4l_announce_v1";
    var dismissed = false;
    try { dismissed = window.localStorage.getItem(KEY) === "1"; } catch(e) {}
    if(window.__s4lAnnounceDismissed) dismissed = true;  // session fallback if storage is blocked
    if(dismissed) return;
    if(document.getElementById("__s4l_announce")) return;

    function mk(tag, parent){ var e=document.createElement(tag); if(parent)parent.appendChild(e); return e; }

    var back = mk("div", document.documentElement); back.id = "__s4l_announce";
    var bs = back.style;
    bs.position="fixed"; bs.top="0"; bs.left="0"; bs.width="100vw"; bs.height="100vh";
    bs.zIndex="2147483647"; bs.display="flex";
    bs.alignItems="center"; bs.justifyContent="center";
    bs.background="rgba(0,0,0,0.55)";
    bs.backdropFilter="blur(3px)"; bs.webkitBackdropFilter="blur(3px)";
    // The ENTIRE modal (backdrop + card + text) is pointer-events:none so that a
    // bot click during this one-time window always passes through to the page,
    // even if the user never clicks OK. The OK button is the ONLY element that
    // re-enables pointer-events, so it stays clickable while everything else is
    // transparent to the automation's CDP/hit-test clicks.
    bs.pointerEvents="none";

    var card = mk("div", back); var cs = card.style;
    cs.pointerEvents="none";
    cs.boxSizing="border-box"; cs.maxWidth="440px"; cs.width="86%";
    cs.padding="26px 26px 22px"; cs.borderRadius="16px";
    cs.background="rgba(20,20,23,0.98)"; cs.color="#fff"; cs.textAlign="center";
    cs.font="14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";
    cs.boxShadow="0 12px 48px rgba(0,0,0,0.55)"; cs.border="1px solid rgba(255,255,255,0.14)";

    var ttl = mk("div", card); ttl.textContent = (payload && payload.title) || "S4L is running";
    ttl.style.fontSize="19px"; ttl.style.fontWeight="700"; ttl.style.letterSpacing="0.3px";
    ttl.style.marginBottom="10px";

    var body = mk("div", card);
    body.textContent = (payload && payload.reassure) || "";
    body.style.opacity="0.82"; body.style.fontSize="14px"; body.style.marginBottom="22px";

    var ok = mk("button", card); ok.textContent="OK";
    var os_ = ok.style;
    os_.pointerEvents="auto";  // the ONLY clickable thing; rest of modal is click-through
    os_.cursor="pointer"; os_.appearance="none"; os_.webkitAppearance="none";
    os_.border="1px solid rgba(255,255,255,0.18)"; os_.borderRadius="10px";
    os_.padding="9px 30px"; os_.fontSize="14px"; os_.fontWeight="600";
    os_.background="#fff"; os_.color="#111"; os_.font="inherit";
    os_.fontWeight="600"; os_.minWidth="120px";
    ok.addEventListener("click", function(){
      try { window.localStorage.setItem(KEY, "1"); } catch(e) {}
      window.__s4lAnnounceDismissed = true;  // session fallback if storage is blocked
      if(back && back.remove) back.remove();
    });
  } catch(e) { /* announcement is best-effort, never throw into the page */ }
};

window.__s4lPaint = function(payload){
  try {
    var ID = "__s4l_overlay";
    var st = window.__s4lOverlayState || (window.__s4lOverlayState = {});
    st.title = payload.title; st.reassure = payload.reassure;
    st.status = payload.status; st.ts = payload.ts || Date.now();

    // Surface the one-time launch notice (carries the reassurance disclaimer).
    try { window.__s4lAnnounce({title: st.title + " is running", reassure: st.reassure}); } catch(e){}

    function mk(tag, parent){ var e=document.createElement(tag); if(parent)parent.appendChild(e); return e; }

    var root = document.getElementById(ID);
    if(!root || !root.isConnected){
      root = mk("div", document.documentElement); root.id = ID;
      var s = root.style;
      // Centered both axes. pointerEvents:none so the overlay can NEVER
      // intercept the automation's clicks: the bot clicks by raw CDP screen
      // coordinates (Input.dispatchMouseEvent) and by Playwright hit-testing,
      // both of which an opaque clickable card sitting over a target would eat.
      s.position="fixed"; s.top="50%"; s.left="50%"; s.transform="translate(-50%,-50%)";
      // Sit one below the announce modal (2147483647) so the one-time "S4L is
      // running" notice + its OK button always stack ON TOP of this always-on
      // status box. They're both screen-centered, so equal z-index would let
      // whichever was appended last (this overlay) cover the OK button.
      s.zIndex="2147483646"; s.pointerEvents="none"; s.maxWidth="460px";
      s.boxSizing="border-box"; s.padding="10px 14px"; s.borderRadius="12px";
      s.background="rgba(15,15,17,0.92)"; s.color="#fff";
      s.font="13px/1.35 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";
      s.boxShadow="0 6px 22px rgba(0,0,0,0.35)"; s.border="1px solid rgba(255,255,255,0.12)";
      s.backdropFilter="blur(6px)"; s.webkitBackdropFilter="blur(6px)";

      var head = mk("div", root); head.style.display="flex"; head.style.alignItems="center"; head.style.gap="8px";
      head.style.cursor="move"; head.style.userSelect="none"; head.style.webkitUserSelect="none";
      var dot = mk("span", head); st._dot = dot;
      dot.style.width="9px"; dot.style.height="9px"; dot.style.borderRadius="50%";
      dot.style.background="#fff"; dot.style.flex="0 0 auto"; dot.style.opacity="1";
      var ttl = mk("span", head); st._title = ttl;
      ttl.style.fontWeight="600"; ttl.style.letterSpacing="0.2px";
      var ago = mk("span", head); st._ago = ago;
      ago.style.marginLeft="auto"; ago.style.opacity="0.55"; ago.style.fontSize="11px";
      ago.style.fontVariantNumeric="tabular-nums";

      var stat = mk("div", root); st._status = stat;
      stat.style.marginTop="6px"; stat.style.fontWeight="500";
      stat.style.whiteSpace="nowrap"; stat.style.overflow="hidden"; stat.style.textOverflow="ellipsis";

      // --- drag-to-move (grab the header) ---------------------------------
      (function(){
        var drag = null; // {dx, dy}
        head.addEventListener("mousedown", function(ev){
          try {
            var r = root.getBoundingClientRect();
            drag = {dx: ev.clientX - r.left, dy: ev.clientY - r.top};
            root.style.transform = "none";
            root.style.left = r.left + "px";
            root.style.top = r.top + "px";
            ev.preventDefault();
          } catch(e) { drag = null; }
        });
        document.addEventListener("mousemove", function(ev){
          if(!drag) return;
          var x = ev.clientX - drag.dx, y = ev.clientY - drag.dy;
          var maxX = Math.max(0, window.innerWidth - root.offsetWidth);
          var maxY = Math.max(0, window.innerHeight - root.offsetHeight);
          root.style.left = Math.min(Math.max(0, x), maxX) + "px";
          root.style.top = Math.min(Math.max(0, y), maxY) + "px";
        });
        document.addEventListener("mouseup", function(){ drag = null; });
      })();

      if(st._iv) clearInterval(st._iv);
      st._iv = setInterval(function(){
        try{
          var dt = Math.max(0, Math.round((Date.now()-st.ts)/1000));
          st._ago.textContent = dt < 1 ? "now" : (dt < 60 ? dt+"s ago" : Math.round(dt/60)+"m ago");
          var stale = dt > 90;                       // fade the dot once activity goes quiet
          var phase = (Date.now()/650) % 2;
          st._dot.style.opacity = stale ? "0.3" : (phase < 1 ? "1" : "0.35");
        }catch(e){}
      }, 250);
    }
    st._title.textContent = st.title;
    st._status.textContent = st.status;
  } catch(e) { /* overlay is best-effort, never throw into the page */ }
};
"""

# Playwright evaluate expression: (re)define the painter, then call it with the
# arg Playwright passes. Used for live updates on existing pages.
PAINT_EXPR = "(payload) => { " + _BODY + " try { window.__s4lPaint(payload); } catch(e){} }"

# Removes the overlay from a page.
CLEAR_EXPR = (
    "() => { var e=document.getElementById('__s4l_overlay'); if(e&&e.remove)e.remove(); "
    "var a=document.getElementById('__s4l_announce'); if(a&&a.remove)a.remove(); "
    "var s=window.__s4lOverlayState; if(s&&s._iv)clearInterval(s._iv); }"
)


def _build_init_script(title: str, reassure: str, status: str) -> str:
    """add_init_script body: define the painter on every new document and seed
    it with the latest known text so a mid-cycle navigation paints instantly."""
    seed = json.dumps({"title": title, "reassure": reassure, "status": status})
    return _BODY + (
        "try { var __p = " + seed + "; __p.ts = Date.now(); window.__s4lPaint(__p); } catch(e){}"
    )


# --- CDP plumbing via Playwright (same path the poster uses) ----------------

class Harness:
    """Thin wrapper that attaches to the harness Chrome over CDP and paints the
    overlay onto every page in the default context. Best-effort throughout."""

    def __init__(self, cdp_url: str = CDP_URL):
        self.cdp_url = cdp_url
        self._pw = None
        self._browser = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=5000)
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def _pages(self):
        pages = []
        for ctx in self._browser.contexts:
            pages.extend(ctx.pages)
        # Only real http(s) tabs; skip about:blank / devtools.
        return [p for p in pages if (p.url or "").startswith("http")]

    def register_init(self, title: str, reassure: str, status: str) -> None:
        """Make the overlay survive navigation: every new document rebuilds it."""
        script = _build_init_script(title, reassure, status)
        for ctx in self._browser.contexts:
            try:
                ctx.add_init_script(script)
            except Exception:
                pass
            # The overlay shares this CDP session with the real twitter-harness
            # automation. When x.com throws up a native JS dialog, both clients'
            # Playwright drivers race to auto-dismiss it (the default behavior
            # when no listener is registered); the loser gets a
            # "Page.handleJavaScriptDialog: No dialog is showing" protocol error
            # that is an UNCAUGHT rejection in the Node driver, killing that
            # whole driver process. A no-op listener opts this connection out of
            # the race entirely (dialog handling stays the automation's job);
            # once the driver's default-dismiss is suppressed here, evaluate()
            # calls on the affected page resume normally as soon as the other
            # side clears the dialog.
            try:
                ctx.on("dialog", lambda dialog: None)
            except Exception:
                pass

    def paint(self, title: str, reassure: str, status: str) -> int:
        """Paint/refresh the overlay on every live page. Returns pages touched."""
        n = 0
        payload = {"title": title, "reassure": reassure, "status": status, "ts": int(time.time() * 1000)}
        for p in self._pages():
            try:
                p.evaluate(PAINT_EXPR, payload)
                n += 1
            except Exception:
                pass
        return n

    def clear(self) -> int:
        n = 0
        for p in self._pages():
            try:
                p.evaluate(CLEAR_EXPR)
                n += 1
            except Exception:
                pass
        return n


# --- cycle-log -> friendly status -------------------------------------------

def _safe_mtime(p: str) -> float:
    """getmtime that tolerates the file vanishing mid-scan (log rotation race).

    The watch loop runs forever while cycles rotate/delete logs underneath it.
    A bare os.path.getmtime on a path that disappeared between the glob and the
    stat raises FileNotFoundError and (previously) killed the whole watcher,
    dropping the overlay until something restarted it. Treat a gone file as
    infinitely old so it just loses the max() race instead of crashing.
    """
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _latest_cycle_log() -> Path | None:
    files = glob.glob(str(LOG_DIR / "twitter-cycle-*.log"))
    if not files:
        return None
    newest = max(files, key=_safe_mtime)
    # The winner could STILL have been deleted between selection and use; the
    # caller (_current_status) stats it again, so hand back None if it's gone.
    return Path(newest) if os.path.exists(newest) else None


_RE_SCAN = re.compile(r"project='([^']+)'\s+q=(['\"])(.*?)\2\s+kept=(\d+)")


def _prettify(line: str) -> str | None:
    """Turn a raw cycle-log line into a short human status, or None to skip."""
    line = line.rstrip()
    if not line.strip():
        return None
    low = line.lower()
    m = _RE_SCAN.search(line)
    if m:
        proj, _q, query, kept = m.group(1), None, m.group(3), m.group(4)
        query = query.strip()
        if len(query) > 48:
            query = query[:47] + "\u2026"
        kept_txt = f" \u00b7 kept {kept}" if kept != "0" else ""
        return f"Scanning X \u00b7 {proj} \u00b7 \u201c{query}\u201d{kept_txt}"
    # A few recognizable phase markers; otherwise show the trimmed tail.
    if "posting" in low or "posted reply" in low:
        return "Posting reply on X\u2026"
    if "drafting" in low or "draft" in low and "cycle" not in low:
        return "Drafting replies\u2026"
    if "scanning" in low or "search" in low:
        return line.strip()[:90]
    # Generic fallback: show the most recent meaningful line, compacted.
    compact = re.sub(r"\s+", " ", line.strip())
    return compact[:90] if compact else None


def _tail_last_meaningful(path: Path, max_scan: int = 200) -> str | None:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 64 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for raw in reversed(data.splitlines()[-max_scan:]):
        pretty = _prettify(raw)
        if pretty:
            return pretty
    return None


def _current_status() -> str:
    log = _latest_cycle_log()
    if not log:
        return "Idle \u2014 waiting for the next cycle\u2026"
    age = time.time() - _safe_mtime(str(log))
    if age > IDLE_AFTER_SEC:
        return "Idle \u2014 waiting for the next cycle\u2026"
    return _tail_last_meaningful(log) or "Working\u2026"


# --- commands ---------------------------------------------------------------

def cmd_install(status: str | None = None) -> int:
    status = status or _current_status()
    try:
        with Harness() as h:
            h.register_init(TITLE, REASSURE, status)
            n = h.paint(TITLE, REASSURE, status)
        print(f"overlay installed on {n} page(s): {status}")
        return 0
    except Exception as e:
        print(f"overlay install failed (harness Chrome down?): {e}", file=sys.stderr)
        return 1


def cmd_status(text: str) -> int:
    try:
        with Harness() as h:
            n = h.paint(TITLE, REASSURE, text)
        print(f"status pushed to {n} page(s): {text}")
        return 0
    except Exception as e:
        print(f"status push failed: {e}", file=sys.stderr)
        return 1


def cmd_clear() -> int:
    try:
        with Harness() as h:
            n = h.clear()
        print(f"overlay cleared on {n} page(s)")
        return 0
    except Exception as e:
        print(f"clear failed: {e}", file=sys.stderr)
        return 1


def cmd_watch(interval: float = 2.0) -> int:
    """Continuously stream the live cycle status into the overlay. Self-healing:
    holds ONE CDP connection open across ticks (light, and friendly to the
    poster's concurrent CDP session) and only reconnects when the harness Chrome
    comes/goes. Never raises into the pipeline."""
    # Singleton guard: there must be exactly ONE watcher painting at a time, or
    # two loops fight over the same overlay (double heartbeat, flicker). Two start
    # lanes can race to spawn this: the MCP's foreground KeepAlive launchd job and
    # the best-effort run-overlay-watch.sh supervisor. Hold an exclusive,
    # non-blocking flock for the life of the process; if another watcher already
    # holds it, exit 0 quietly and let that one own the overlay. The lock fd is
    # intentionally leaked (kept open) until the process dies so the OS releases
    # it automatically on exit/kill.
    try:
        _lock_fd = os.open("/tmp/s4l_overlay_watch.lock", os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another overlay watcher already running; exiting", file=sys.stderr)
        return 0
    print(f"watching {LOG_DIR}/twitter-cycle-*.log -> overlay on {CDP_URL} (Ctrl-C to stop)")
    # Treat SIGTERM (launchd unload, `kill`) like Ctrl-C so the overlay is
    # cleared on the way out instead of lingering until the next navigation.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    # Hard watchdog: a wedged CDP call (e.g. a page whose JS is paused on a
    # native dialog) can block the loop body indefinitely without ever raising,
    # so the normal try/except reconnect logic below never gets a chance to
    # run. That let a past incident spin a full core for ~3.5 hours with the
    # overlay silently stuck instead of failing loudly. A background thread
    # that self-terminates the process if the main loop hasn't ticked in too
    # long is the only thing that reliably fires even when the main thread is
    # stuck inside a blocking Playwright/CDP call; os._exit() skips Python-level
    # cleanup on purpose since a stuck process can't be trusted to run it
    # either. launchd's KeepAlive relaunches a clean instance immediately.
    WATCHDOG_MAX_STALL_SEC = 60.0
    _last_tick = {"t": time.time()}

    def _watchdog() -> None:
        while True:
            time.sleep(10)
            stalled = time.time() - _last_tick["t"]
            if stalled > WATCHDOG_MAX_STALL_SEC:
                print(
                    f"[watchdog] loop stalled {stalled:.0f}s (>{WATCHDOG_MAX_STALL_SEC:.0f}s); "
                    "self-terminating for a clean launchd restart",
                    file=sys.stderr,
                )
                os._exit(1)

    threading.Thread(target=_watchdog, daemon=True).start()

    last_status = None
    h: Harness | None = None
    registered = False
    try:
        while True:
            _last_tick["t"] = time.time()
            # Never let status computation (log globbing/stat, all racing against
            # live log rotation) kill the watcher; fall back to a neutral status.
            try:
                status = _current_status()
            except Exception:
                status = "Working\u2026"
            try:
                if h is None:
                    h = Harness().__enter__()
                    registered = False
                if not registered:
                    # Re-register init on each (re)connect so fresh tabs inherit it.
                    h.register_init(TITLE, REASSURE, status)
                    registered = True
                # Repaint every tick even when text is unchanged: the timestamp
                # reset keeps the heartbeat fresh so the dot never looks dead.
                if h.paint(TITLE, REASSURE, status) == 0:
                    # No live page (all tabs closed/navigating) -> drop & retry.
                    raise RuntimeError("no live page")
            except Exception:
                # Harness down or transient CDP hiccup; tear down and retry next tick.
                if h is not None:
                    try:
                        h.__exit__(None, None, None)
                    except Exception:
                        pass
                    h = None
                registered = False
            if status != last_status:
                print(f"[{time.strftime('%H:%M:%S')}] {status}")
                last_status = status
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopping watch; clearing overlay")
    finally:
        if h is not None:
            try:
                h.clear()
            except Exception:
                pass
            try:
                h.__exit__(None, None, None)
            except Exception:
                pass
        else:
            try:
                cmd_clear()
            except Exception:
                pass
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "install":
        return cmd_install(argv[1] if len(argv) > 1 else None)
    if cmd == "status":
        if len(argv) < 2:
            print("usage: harness_overlay.py status \"text\"", file=sys.stderr)
            return 2
        return cmd_status(argv[1])
    if cmd == "clear":
        return cmd_clear()
    if cmd == "watch":
        iv = float(argv[1]) if len(argv) > 1 else 2.0
        return cmd_watch(iv)
    print(f"unknown command: {cmd}", file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
