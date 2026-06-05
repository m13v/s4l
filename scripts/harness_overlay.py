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
import json
import os
import re
import signal
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
# `_BODY` defines window.__sapsPaint(payload): idempotently creates the overlay
# DOM, then updates its text. A lone setInterval drives both the pulse and the
# "updated Ns ago" ticker so the overlay always looks alive between status
# pushes. Built with createElement + element.style.<prop> + textContent only
# (CSP-safe; no <style> tag, no innerHTML-with-style-attrs). pointer-events is
# none so the overlay can never intercept the automation's own clicks.
_BODY = r"""
window.__sapsPaint = function(payload){
  try {
    var ID = "__saps_overlay";
    var st = window.__sapsOverlayState || (window.__sapsOverlayState = {});
    st.title = payload.title; st.reassure = payload.reassure;
    st.status = payload.status; st.ts = payload.ts || Date.now();

    function mk(tag, parent){ var e=document.createElement(tag); if(parent)parent.appendChild(e); return e; }

    var root = document.getElementById(ID);
    if(!root || !root.isConnected){
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
          var stale = dt > 90;                       // fade the dot once activity goes quiet
          var phase = (Date.now()/650) % 2;
          st._dot.style.opacity = stale ? "0.3" : (phase < 1 ? "1" : "0.35");
        }catch(e){}
      }, 250);
    }
    st._title.textContent = st.title;
    st._reassure.textContent = st.reassure;
    st._status.textContent = st.status;
  } catch(e) { /* overlay is best-effort, never throw into the page */ }
};
"""

# Playwright evaluate expression: (re)define the painter, then call it with the
# arg Playwright passes. Used for live updates on existing pages.
PAINT_EXPR = "(payload) => { " + _BODY + " try { window.__sapsPaint(payload); } catch(e){} }"

# Removes the overlay from a page.
CLEAR_EXPR = (
    "() => { var e=document.getElementById('__saps_overlay'); if(e&&e.remove)e.remove(); "
    "var s=window.__sapsOverlayState; if(s&&s._iv)clearInterval(s._iv); }"
)


# --- the interactive draft sidebar ------------------------------------------
# A left-edge panel that lists the drafts waiting to post. Unlike the status
# overlay (which is pointer-events:none and purely cosmetic), the sidebar is
# INTERACTIVE: pointer-events:auto, buttons the user can click. Because a button
# runs in the page's JS world and cannot call Python/CDP directly, the click
# bridge is a poll: a click stashes {id, ts} on window.__sapsClick, and the
# Python watch loop reads + clears it each tick, then drives the preview
# (navigate to the tweet + type the draft into the reply box, NEVER submit).
#
# Same CSP discipline as the status overlay: createElement + element.style.<prop>
# + textContent only, click handlers via addEventListener (NOT inline attrs / no
# innerHTML). The two elements are kept deliberately separate so the sidebar's
# pointer-events:auto can never bleed into the cosmetic status overlay and start
# intercepting the automation's own clicks.
_SIDEBAR_BODY = r"""
window.__sapsPaintSidebar = function(payload){
  try {
    var ID = "__saps_sidebar";
    var drafts = (payload && payload.drafts) || [];
    var note = (payload && payload.note) || "";
    function mk(tag, parent){ var e=document.createElement(tag); if(parent)parent.appendChild(e); return e; }

    var root = document.getElementById(ID);
    var rebuilt = false;
    if(!root || !root.isConnected){
      root = mk("div", document.documentElement); root.id = ID;
      var s = root.style;
      s.position="fixed"; s.top="0"; s.left="0"; s.height="100vh"; s.width="264px";
      s.zIndex="2147483646"; s.pointerEvents="auto"; s.boxSizing="border-box";
      s.padding="14px 12px"; s.overflowY="auto";
      s.background="rgba(15,15,17,0.96)"; s.color="#fff";
      s.font="13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";
      s.borderRight="1px solid rgba(255,255,255,0.12)";
      s.boxShadow="2px 0 18px rgba(0,0,0,0.40)";
      s.backdropFilter="blur(6px)"; s.webkitBackdropFilter="blur(6px)";
      rebuilt = true;
    }
    var st = window.__sapsSidebarState || (window.__sapsSidebarState = {});
    var sig = drafts.map(function(d){return d.id;}).join(",");
    if(rebuilt || st.sig !== sig){
      st.sig = sig;
      while(root.firstChild) root.removeChild(root.firstChild);

      var head = mk("div", root);
      head.style.display="flex"; head.style.alignItems="center"; head.style.gap="8px";
      var ttl = mk("span", head); ttl.textContent="Drafts to post";
      ttl.style.fontWeight="600";
      var cnt = mk("span", head); cnt.textContent=String(drafts.length);
      cnt.style.marginLeft="auto"; cnt.style.opacity="0.55"; cnt.style.fontSize="11px";
      cnt.style.fontVariantNumeric="tabular-nums";

      var sub = mk("div", root);
      sub.textContent="Click one to load it into the reply box. It won\u2019t post.";
      sub.style.opacity="0.6"; sub.style.fontSize="11px"; sub.style.margin="3px 0 10px";

      var noteEl = mk("div", root); st._note = noteEl; noteEl.id="__saps_sb_note";
      noteEl.style.minHeight="14px"; noteEl.style.fontSize="11px";
      noteEl.style.opacity="0.85"; noteEl.style.marginBottom="10px";
      noteEl.textContent = note;

      if(!drafts.length){
        var empty = mk("div", root);
        empty.textContent="No drafts waiting. Run a draft cycle.";
        empty.style.opacity="0.5"; empty.style.fontSize="12px";
      }
      drafts.forEach(function(d){
        var b = mk("div", root); var bs = b.style;
        bs.cursor="pointer"; bs.padding="9px 10px"; bs.marginBottom="8px";
        bs.borderRadius="10px"; bs.border="1px solid rgba(255,255,255,0.10)";
        bs.background=(window.__sapsSelectedId==d.id)?"rgba(255,255,255,0.16)":"rgba(255,255,255,0.06)";
        b.setAttribute("data-saps-id", String(d.id));

        var meta = mk("div", b);
        meta.style.display="flex"; meta.style.gap="6px"; meta.style.alignItems="center";
        meta.style.marginBottom="4px";
        if(d.project){
          var proj = mk("span", meta); proj.textContent=d.project;
          proj.style.fontSize="10px"; proj.style.fontWeight="600";
          proj.style.padding="1px 6px"; proj.style.borderRadius="6px";
          proj.style.background="rgba(255,255,255,0.14)";
        }
        if(d.handle){
          var who = mk("span", meta); who.textContent="@"+d.handle;
          who.style.fontSize="11px"; who.style.opacity="0.6";
          who.style.overflow="hidden"; who.style.textOverflow="ellipsis"; who.style.whiteSpace="nowrap";
        }
        var txt = mk("div", b);
        txt.textContent = d.draft_text || "(no draft text)";
        txt.style.fontSize="12px"; txt.style.lineHeight="1.35";
        txt.style.display="-webkit-box"; txt.style.webkitLineClamp="3";
        txt.style.webkitBoxOrient="vertical"; txt.style.overflow="hidden";

        b.addEventListener("mouseenter", function(){ bs.background="rgba(255,255,255,0.12)"; });
        b.addEventListener("mouseleave", function(){
          bs.background=(window.__sapsSelectedId==d.id)?"rgba(255,255,255,0.16)":"rgba(255,255,255,0.06)";
        });
        b.addEventListener("click", function(){
          window.__sapsClick = {id: d.id, ts: Date.now()};
          window.__sapsSelectedId = d.id;
          var all = root.querySelectorAll("[data-saps-id]");
          for(var i=0;i<all.length;i++){ all[i].style.background="rgba(255,255,255,0.06)"; }
          bs.background="rgba(255,255,255,0.16)";
          if(st._note) st._note.textContent="Loading draft into the reply box\u2026";
        });
      });
    }
    if(st._note && typeof note === "string" && note.length) st._note.textContent = note;
  } catch(e) { /* sidebar is best-effort, never throw into the page */ }
};
"""

SIDEBAR_PAINT_EXPR = "(payload) => { " + _SIDEBAR_BODY + " try { window.__sapsPaintSidebar(payload); } catch(e){} }"

# Update ONLY the note line without rebuilding the button list (cheap, called on
# preview start / success / error).
SB_NOTE_EXPR = (
    "(note) => { try { var e=document.getElementById('__saps_sb_note'); "
    "if(e) e.textContent=note; } catch(e){} }"
)

# Read-and-clear the pending click set by a sidebar button.
READ_CLICK_EXPR = "() => { var c = window.__sapsClick || null; window.__sapsClick = null; return c; }"

CLEAR_SB_EXPR = "() => { var e=document.getElementById('__saps_sidebar'); if(e&&e.remove)e.remove(); }"

# Reply composer selectors (mirrors twitter_browser._wait_for_reply_textbox).
_REPLY_SELECTORS = (
    '[data-testid="tweetTextarea_0"]',
    '[role="textbox"][aria-label="Post text"]',
    '[role="textbox"][aria-label="Tweet your reply"]',
    '[role="textbox"][aria-label="Post your reply"]',
)

# Whether the sidebar is enabled at all (set SAPS_SIDEBAR=0 to disable).
SIDEBAR_ENABLED = os.environ.get("SAPS_SIDEBAR", "1").strip() != "0"
# How often (seconds) to re-fetch the drafts list from the API.
SIDEBAR_REFRESH_SEC = int(os.environ.get("SAPS_SIDEBAR_REFRESH_SEC", "12"))


def _build_init_script(title: str, reassure: str, status: str) -> str:
    """add_init_script body: define the painter on every new document and seed
    it with the latest known text so a mid-cycle navigation paints instantly."""
    seed = json.dumps({"title": title, "reassure": reassure, "status": status})
    return _BODY + (
        "try { var __p = " + seed + "; __p.ts = Date.now(); window.__sapsPaint(__p); } catch(e){}"
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

    # --- interactive sidebar -------------------------------------------------

    def register_sidebar_init(self, drafts: list) -> None:
        """Rebuild the sidebar on every new document so it survives navigation."""
        seed = json.dumps({"drafts": drafts, "note": ""})
        script = _SIDEBAR_BODY + ("try { window.__sapsPaintSidebar(" + seed + "); } catch(e){}")
        for ctx in self._browser.contexts:
            try:
                ctx.add_init_script(script)
            except Exception:
                pass

    def paint_sidebar(self, drafts: list, note: str = "") -> int:
        n = 0
        payload = {"drafts": drafts, "note": note}
        for p in self._pages():
            try:
                p.evaluate(SIDEBAR_PAINT_EXPR, payload)
                n += 1
            except Exception:
                pass
        return n

    def set_sidebar_note(self, note: str) -> None:
        for p in self._pages():
            try:
                p.evaluate(SB_NOTE_EXPR, note)
            except Exception:
                pass

    def read_click(self):
        """Return (click_dict, page) for the first pending sidebar click, else (None, None)."""
        for p in self._pages():
            try:
                c = p.evaluate(READ_CLICK_EXPR)
                if c:
                    return c, p
            except Exception:
                pass
        return None, None

    def clear_sidebar(self) -> int:
        n = 0
        for p in self._pages():
            try:
                p.evaluate(CLEAR_SB_EXPR)
                n += 1
            except Exception:
                pass
        return n

    def _wait_reply_box(self, page, total_ms: int = 30000):
        import time as _t
        deadline = _t.monotonic() + total_ms / 1000.0
        while _t.monotonic() < deadline:
            for sel in _REPLY_SELECTORS:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:
                    pass
            page.wait_for_timeout(500)
        return None

    def preview_draft(self, page, tweet_url: str, text: str) -> dict:
        """Navigate to the tweet and TYPE the draft into the reply box.

        Deliberately stops before submitting: there is no click on the Reply /
        tweetButtonInline button anywhere in this method. It mirrors the
        navigate+locate+type prefix of twitter_browser.reply_to_tweet, minus the
        post step, so the user sees exactly how the reply would look without it
        going live.
        """
        try:
            try:
                page.goto(tweet_url, wait_until="load", timeout=60000)
            except Exception:
                try:
                    page.goto(tweet_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
            page.wait_for_timeout(2500)
            try:
                page.wait_for_selector("main", state="attached", timeout=20000)
            except Exception:
                pass
            box = self._wait_reply_box(page, 30000)
            if not box:
                return {"ok": False, "error": "reply_box_not_found"}
            box.click()
            page.wait_for_timeout(400)
            # Clear anything already in the composer so a re-preview is clean.
            try:
                page.keyboard.press("Meta+A")
                page.keyboard.press("Delete")
            except Exception:
                pass
            page.keyboard.type(text, delay=8)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# --- drafts data source -----------------------------------------------------

def _fetch_drafts(limit: int = 40) -> list:
    """Fetch the drafts waiting to post (status='drafted') via the s4l.ai API.

    Returns a compact list the sidebar can render. Best-effort: any failure
    (API down, no identity, SSL hiccup) returns [] so the watch loop never
    crashes the pipeline over a missing sidebar list.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get
        resp = api_get(
            "/api/v1/twitter-candidates",
            query={"status": "drafted", "limit": str(limit)},
        )
    except Exception:
        return []
    rows = (resp.get("data") or {}).get("candidates") if isinstance(resp, dict) else None
    rows = rows or (resp.get("candidates") if isinstance(resp, dict) else None) or []
    drafts = []
    for r in rows:
        text = (r.get("draft_reply_text") or "").strip()
        url = r.get("tweet_url") or ""
        if not text or not url:
            continue
        drafts.append({
            "id": r.get("id"),
            "project": r.get("matched_project") or "",
            "handle": r.get("author_handle") or "",
            "tweet_url": url,
            "draft_text": text,
        })
    return drafts


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
    holds ONE CDP connection open across ticks (light, and friendly to the
    poster's concurrent CDP session) and only reconnects when the harness Chrome
    comes/goes. Never raises into the pipeline."""
    print(f"watching {LOG_DIR}/twitter-cycle-*.log -> overlay on {CDP_URL} (Ctrl-C to stop)")
    if SIDEBAR_ENABLED:
        print(f"interactive drafts sidebar ON (refresh every {SIDEBAR_REFRESH_SEC}s; SAPS_SIDEBAR=0 to disable)")
    # Treat SIGTERM (launchd unload, `kill`) like Ctrl-C so the overlay is
    # cleared on the way out instead of lingering until the next navigation.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    last_status = None
    h: Harness | None = None
    registered = False
    drafts: list = []
    drafts_by_id: dict = {}
    last_sb_fetch = 0.0
    last_sb_sig = None
    try:
        while True:
            status = _current_status()
            try:
                if h is None:
                    h = Harness().__enter__()
                    registered = False
                if not registered:
                    # Re-register init on each (re)connect so fresh tabs inherit it.
                    h.register_init(TITLE, REASSURE, status)
                    if SIDEBAR_ENABLED:
                        h.register_sidebar_init(drafts)
                    registered = True
                # Repaint every tick even when text is unchanged: the timestamp
                # reset keeps the heartbeat fresh so the dot never looks dead.
                if h.paint(TITLE, REASSURE, status) == 0:
                    # No live page (all tabs closed/navigating) -> drop & retry.
                    raise RuntimeError("no live page")

                if SIDEBAR_ENABLED:
                    now = time.time()
                    if now - last_sb_fetch >= SIDEBAR_REFRESH_SEC:
                        drafts = _fetch_drafts()
                        drafts_by_id = {str(d["id"]): d for d in drafts}
                        last_sb_fetch = now
                        sig = ",".join(str(d["id"]) for d in drafts)
                        if sig != last_sb_sig:
                            # List changed -> re-register init (fresh tabs) + repaint.
                            h.register_sidebar_init(drafts)
                            last_sb_sig = sig
                    h.paint_sidebar(drafts)

                    # Click bridge: a sidebar button stashed {id, ts} -> drive preview.
                    click, click_page = h.read_click()
                    if click and click_page is not None:
                        did = str(click.get("id"))
                        d = drafts_by_id.get(did)
                        if d is None:
                            h.set_sidebar_note("That draft is no longer in the list.")
                        elif "posting" in status.lower():
                            # Collision guard: a posting step is driving the same
                            # Chrome right now. Refuse so we don't fight it.
                            h.set_sidebar_note("Busy posting right now \u2014 try again in a moment.")
                        else:
                            short = (d["draft_text"][:40] + "\u2026") if len(d["draft_text"]) > 40 else d["draft_text"]
                            h.set_sidebar_note("Opening tweet + typing draft\u2026")
                            print(f"[{time.strftime('%H:%M:%S')}] preview draft id={did} -> {d['tweet_url']}")
                            res = h.preview_draft(click_page, d["tweet_url"], d["draft_text"])
                            if res.get("ok"):
                                h.set_sidebar_note(f"\u2713 Loaded into reply box (not posted): \u201c{short}\u201d")
                            else:
                                h.set_sidebar_note(f"Couldn\u2019t load draft: {res.get('error')}")
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
