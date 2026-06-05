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

import glob
import os
import re
import sys
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
    if os.environ.get("_SAPS_OVERLAY_REEXEC") == "1":
        return  # already tried; fall through and let the import error surface
    for cand in (
        "/opt/homebrew/bin/python3.11",
        "/usr/bin/python3",
        "/opt/homebrew/bin/python3",
    ):
        if Path(cand).exists() and os.path.realpath(cand) != os.path.realpath(sys.executable):
            env = dict(os.environ, _SAPS_OVERLAY_REEXEC="1")
            os.execve(cand, [cand, os.path.abspath(__file__), *sys.argv[1:]], env)


_ensure_playwright_interpreter()

# --- config -----------------------------------------------------------------

CDP_URL = os.environ.get("TWITTER_CDP_URL", "http://127.0.0.1:9555").strip()
LOG_DIR = Path(os.environ.get("SAPS_LOG_DIR", str(Path.home() / "social-autoposter" / "skill" / "logs")))
# How stale a cycle log can be (seconds) before we treat the harness as idle.
IDLE_AFTER_SEC = int(os.environ.get("SAPS_OVERLAY_IDLE_SEC", "240"))

TITLE = "Social Autoposter"
REASSURE = (
    "Working in the background. You can keep using other apps and leave this "
    "window behind \u2014 just don\u2019t close it."
)

# --- the page-side overlay builder ------------------------------------------
# A single JS function installed on `window`. Called with the current title,
# reassurance line, status line, and an epoch ms timestamp. Idempotent: it
# creates the DOM on first call and only updates text thereafter. A lone
# setInterval drives both the pulse and the "updated Ns ago" ticker so the
# overlay always looks alive between status pushes.
OVERLAY_JS = r"""
(function(payload){
  try {
    var ID = "__saps_overlay";
    var st = window.__sapsOverlayState || (window.__sapsOverlayState = {});
    st.title = payload.title; st.reassure = payload.reassure;
    st.status = payload.status; st.ts = payload.ts || Date.now();

    function mk(tag, parent){ var e=document.createElement(tag); if(parent)parent.appendChild(e); return e; }

    var root = document.getElementById(ID);
    if(!root){
      root = mk("div", document.documentElement); root.id = ID;
      var s = root.style;
      s.position="fixed"; s.top="12px"; s.left="50%"; s.transform="translateX(-50%)";
      s.zIndex="2147483647"; s.pointerEvents="none"; s.maxWidth="460px";
      s.boxSizing="border-box"; s.padding="10px 14px"; s.borderRadius="12px";
      s.background="rgba(15,15,17,0.92)"; s.color="#fff";
      s.font="13px/1.35 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";
      s.boxShadow="0 6px 22px rgba(0,0,0,0.35)"; s.border="1px solid rgba(255,255,255,0.12)";
      s.backdropFilter="blur(6px)"; s.webkitBackdropFilter="blur(6px)";

      var head = mk("div", root); head.style.display="flex"; head.style.alignItems="center"; head.style.gap="8px";
      var dot = mk("span", head); st._dot = dot;
      dot.style.width="9px"; dot.style.height="9px"; dot.style.borderRadius="50%";
      dot.style.background="#fff"; dot.style.flex="0 0 auto"; dot.style.opacity="1";
      var ttl = mk("span", head); st._title = ttl;
      ttl.style.fontWeight="600"; ttl.style.letterSpacing="0.2px";
      var ago = mk("span", head); st._ago = ago;
      ago.style.marginLeft="auto"; ago.style.opacity="0.55"; ago.style.fontSize="11px";
      ago.style.fontVariantNumeric="tabular-nums";

      var re = mk("div", root); st._reassure = re;
      re.style.marginTop="5px"; re.style.opacity="0.72"; re.style.fontSize="12px";

      var stat = mk("div", root); st._status = stat;
      stat.style.marginTop="6px"; stat.style.fontWeight="500";
      stat.style.whiteSpace="nowrap"; stat.style.overflow="hidden"; stat.style.textOverflow="ellipsis";

      if(st._iv) clearInterval(st._iv);
      st._iv = setInterval(function(){
        try{
          var dt = Math.max(0, Math.round((Date.now()-st.ts)/1000));
          st._ago.textContent = dt < 1 ? "now" : (dt < 60 ? dt+"s ago" : Math.round(dt/60)+"m ago");
          // breathe the dot; fade it once activity goes stale
          var stale = dt > 90;
          var phase = (Date.now()/650) % 2;
          st._dot.style.opacity = stale ? "0.3" : (phase < 1 ? "1" : "0.35");
        }catch(e){}
      }, 250);
    }
    st._title.textContent = st.title;
    st._reassure.textContent = st.reassure;
    st._status.textContent = st.status;
  } catch(e) { /* overlay is best-effort, never throw into the page */ }
})(arguments[0]);
"""

# The same builder, wrapped so add_init_script can register it for every future
# document. On a fresh document there is no status yet, so it seeds a generic
# "starting up" line; the watch loop overwrites it within a couple seconds.
INIT_SCRIPT = (
    "window.__sapsOverlayBuild = function(p){ var f = "
    + OVERLAY_JS.strip()
    + "; };\n"
    "try { window.__sapsOverlayBuild(); } catch(e){}\n"
)


def _build_init_script(title: str, reassure: str, status: str) -> str:
    # Register a re-runner on new documents that paints with the latest known
    # text. We inline the payload so even a navigation that happens between
    # watch ticks shows the right thing immediately.
    payload = {
        "title": title.replace("\\", "\\\\").replace('"', '\\"'),
        "reassure": reassure.replace("\\", "\\\\").replace('"', '\\"'),
        "status": status.replace("\\", "\\\\").replace('"', '\\"'),
    }
    fn = "(function(p)" + OVERLAY_JS.strip()[len("(function(payload)"):]
    return (
        f'var __p = {{title:"{payload["title"]}",reassure:"{payload["reassure"]}",'
        f'status:"{payload["status"]}",ts:Date.now()}};\n'
        f'({fn})(__p);\n'
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

    def paint(self, title: str, reassure: str, status: str) -> int:
        """Paint/refresh the overlay on every live page. Returns pages touched."""
        n = 0
        payload = {"title": title, "reassure": reassure, "status": status, "ts": int(time.time() * 1000)}
        for p in self._pages():
            try:
                p.evaluate(OVERLAY_JS, payload)
                n += 1
            except Exception:
                pass
        return n

    def clear(self) -> int:
        n = 0
        js = "(function(){var e=document.getElementById('__saps_overlay');if(e&&e.remove)e.remove();var s=window.__sapsOverlayState;if(s&&s._iv)clearInterval(s._iv);})()"
        for p in self._pages():
            try:
                p.evaluate(js)
                n += 1
            except Exception:
                pass
        return n


# --- cycle-log -> friendly status -------------------------------------------

def _latest_cycle_log() -> Path | None:
    files = glob.glob(str(LOG_DIR / "twitter-cycle-*.log"))
    if not files:
        return None
    newest = max(files, key=lambda f: os.path.getmtime(f))
    return Path(newest)


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
    age = time.time() - os.path.getmtime(log)
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
    reconnects when the harness Chrome comes and goes; never raises."""
    print(f"watching {LOG_DIR}/twitter-cycle-*.log -> overlay on {CDP_URL} (Ctrl-C to stop)")
    last_status = None
    while True:
        try:
            status = _current_status()
            with Harness() as h:
                # Re-register init on every (re)connect so fresh tabs are covered.
                h.register_init(TITLE, REASSURE, status)
                # Keep painting even when text is unchanged: the timestamp reset
                # keeps the heartbeat fresh so the dot never looks dead.
                h.paint(TITLE, REASSURE, status)
            if status != last_status:
                print(f"[{time.strftime('%H:%M:%S')}] {status}")
                last_status = status
        except KeyboardInterrupt:
            print("\nstopping watch; clearing overlay")
            try:
                cmd_clear()
            except Exception:
                pass
            return 0
        except Exception:
            # Harness down or transient CDP hiccup; back off and retry.
            pass
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            cmd_clear()
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
