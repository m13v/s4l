#!/usr/bin/env python3
"""setup_twitter_auth.py - Twitter/X session bootstrap for the MCP setup flow.

Used by the social-autoposter MCP `setup` tool (action=connect_x) to give a
brand-new user a logged-in X session in the autoposter's managed browser WITHOUT
making them paste cookies or hand-edit anything.

It answers the three questions the setup flow needs:
  1. Do cookies already exist in the managed browser?  (is it logged in?)
  2. Are they still valid?                              (auth_token present after
                                                          a real x.com/home load)
  3. Does the user need to re-log in manually?          (import failed / no source)

How it works
------------
The autoposter posts through a managed REAL Google Chrome on CDP port 9555 with a
persistent profile at ~/.claude/browser-profiles/browser-harness (same Chrome the
twitter-harness pipeline drives). This helper:

  status  - probe that Chrome; if up, report whether the X session is valid.
  connect - ensure that Chrome is running; if the X session is already valid,
            no-op; otherwise IMPORT x.com/twitter.com cookies from the user's
            everyday browser (Chrome/Arc/Brave/Edge, auto-detected) via
            ai_browser_profile.cookies, then re-validate. If still logged out,
            report needs_login so the caller can ask the user to sign in once in
            the (now on-screen) managed Chrome window.

Only x.com + twitter.com cookies are copied. No other site's session is touched,
and cookie VALUES are never printed.

Output: a single JSON object on stdout. Human-readable notes go to stderr.

CLI:
  python3 setup_twitter_auth.py status
  python3 setup_twitter_auth.py connect [--source chrome:Default] [--no-launch]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# websocket-client is needed for CDP (status/connect). It is NOT needed for
# `detect-sources` (pure filesystem), so don't hard-exit at import time — defer
# the error to the commands that actually attach to Chrome.
try:
    from websocket import create_connection  # websocket-client
    _WEBSOCKET_IMPORT_ERROR = None
except ImportError:
    create_connection = None  # type: ignore[assignment]
    _WEBSOCKET_IMPORT_ERROR = (
        "websocket-client not installed (needed for CDP). pip install websocket-client"
    )

# Optional server-side session-cookie store (best-effort). Lets connect_x persist
# the validated X cookies so restore_twitter_session.py can auto-re-inject them
# after any logout. Guarded so a missing dep or offline API never breaks setup.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from http_api import api_post  # noqa: E402
    from twitter_account import resolve_handle  # noqa: E402
except Exception:
    api_post = None
    resolve_handle = None

# Local 0600 cookie mirror — the keychain-independent durability layer (Gap B).
# Always importable (stdlib only); guarded so a path quirk never breaks setup.
try:
    import twitter_cookie_mirror  # noqa: E402
except Exception:
    twitter_cookie_mirror = None

# Vendored cookie copier — also gives us stdlib-only browser/profile detection
# (detect_browsers, copy_db) used to (a) pick the RIGHT browser to import from so
# we trigger exactly ONE keychain prompt, and (b) populate the panel's
# "import from" dropdown. These helpers touch the filesystem only (no keychain
# read, no decryption), so importing/using them never shows a Safe Storage prompt.
try:
    import copy_browser_cookies as _cbc  # noqa: E402
except Exception:
    _cbc = None

# --- Config -----------------------------------------------------------------

# Same managed Chrome the twitter-harness pipeline uses (skill/lib/twitter-backend.sh).
CDP = os.environ.get("SAPS_TWITTER_CDP_URL", os.environ.get("TWITTER_CDP_URL", "http://127.0.0.1:9555")).rstrip("/")
PORT = int(CDP.rsplit(":", 1)[-1]) if CDP.rsplit(":", 1)[-1].isdigit() else 9555
PROFILE_DIR = Path.home() / ".claude" / "browser-profiles" / "browser-harness"
# Same PID file server.py (the twitter-harness MCP) writes, so a Chrome launched
# here is tracked and reapable by bh_stop instead of becoming an orphan that
# strands the debug port.
PID_FILE = Path.home() / ".claude" / "browser-profiles" / "browser-harness.chrome.pid"

# Browsers ai_browser_profile.cookies can read from, in auto-detect priority.
AUTO_SOURCES = ["chrome:Default", "arc:Default", "brave:Default", "edge:Default"]
DOMAINS = "x.com,twitter.com"

# Primary cookie copier: a self-contained, dependency-light script that ships
# WITH this repo (deps already in requirements.txt: cryptography +
# websocket-client). This is what makes the auto-import work on a fresh install.
VENDORED_COOKIE_SCRIPT = Path(__file__).resolve().parent / "copy_browser_cookies.py"

# Legacy fallback: the separate ~/ai-browser-profile project. Only present on
# the maintainer's dev box; never installed on a customer machine. Kept solely
# so nothing regresses there if the vendored script is somehow missing.
ABP_PYTHON = Path.home() / "ai-browser-profile" / ".venv" / "bin" / "python"


# --- Chrome lifecycle -------------------------------------------------------

def _port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _cdp_alive() -> bool:
    if not _port_open(PORT):
        return False
    try:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=1.5) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _resolve_chrome_bin() -> str | None:
    env = os.environ.get("BH_CHROME_BIN")
    if env and Path(env).exists():
        return env
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    import shutil
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _launch_chrome() -> bool:
    """Launch the managed Chrome on PORT, ON-SCREEN (so manual login is possible).

    Deliberately does NOT use the off-screen window-position the cron pipeline
    uses (BH_WINDOW_POS 3042,-1032 is a multi-monitor placement); during setup
    the user may need to see this window to log in. Cookies persist on disk, so
    later headless/off-screen relaunches by the pipeline inherit the session.
    """
    chrome = _resolve_chrome_bin()
    if not chrome:
        return False
    cmd = [
        chrome,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        # Encrypt the cookie store with Chrome's fixed obfuscation key instead of
        # the macOS Keychain ("Chrome Safe Storage"). Without this, a keychain
        # lock/re-lock leaves Chrome unable to decrypt its Cookies SQLite on the
        # next launch and the imported session is discarded. Must match the cycle
        # launcher (skill/lib/twitter-backend.sh) so the session connected here
        # actually survives the pipeline's later relaunches. (Persistence fix,
        # 2026-06-02.)
        "--password-store=basic",
        "--use-mock-keychain",
        "--disable-features=ChromeWhatsNewUI",
    ]
    is_linux = sys.platform.startswith("linux")
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if is_linux:
        cmd += ["--no-sandbox", "--disable-dev-shm-usage"]
        if not has_display:
            cmd += ["--headless=new", "--disable-gpu"]
    else:
        # macOS: place the window on-screen, top-left, so the user can sign in.
        cmd += ["--window-position=80,80", "--window-size=1100,900"]
    cmd.append("about:blank")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        PID_FILE.write_text(str(proc.pid))
    except OSError:
        pass
    for _ in range(15):
        if _cdp_alive():
            return True
        time.sleep(1)
    return _cdp_alive()


def ensure_chrome(launch: bool = True) -> bool:
    if _cdp_alive():
        return True
    if not launch:
        return False
    return _launch_chrome()


# --- CDP attach + login validation (mirrors restore_twitter_session.py) -----

def _attach():
    targets = json.load(urllib.request.urlopen(f"{CDP}/json", timeout=10))
    page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        page = json.load(urllib.request.urlopen(
            urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT"), timeout=10))
    ws = create_connection(page["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    state = {"id": 0}

    def send(method, params=None):
        state["id"] += 1
        ws.send(json.dumps({"id": state["id"], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == state["id"]:
                return msg
    return ws, send


def _current_url(send) -> str:
    r = send("Runtime.evaluate", {"expression": "location.href", "returnByValue": True})
    return (r.get("result", {}).get("result", {}) or {}).get("value", "") or ""


def _has_auth_cookie(send) -> bool:
    r = send("Network.getAllCookies")
    cks = r.get("result", {}).get("cookies", []) or []
    return any(
        c.get("name") == "auth_token" and "x.com" in (c.get("domain") or "")
        for c in cks
    )


def _logged_in(send) -> bool:
    send("Network.enable")
    if _has_auth_cookie(send):
        return True
    send("Page.enable")
    send("Page.navigate", {"url": "https://x.com/home"})
    for _ in range(15):
        time.sleep(1)
        if _has_auth_cookie(send):
            return True
        u = _current_url(send)
        if "/login" in u or "/i/flow/login" in u or u.rstrip("/") == "https://x.com":
            return False
    return _has_auth_cookie(send)


def _is_session_valid() -> bool:
    """Rigorous check: navigates x.com/home if needed. Used by `connect`."""
    ws, send = _attach()
    try:
        return _logged_in(send)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _has_session_quick() -> bool:
    """Read-only check: auth_token cookie present? Never navigates the live
    browser, so it's safe to poll while a posting cycle is running. Used by
    `status`. A present-but-server-revoked cookie can false-positive here; the
    `connect` path's navigate-validate is the authoritative check."""
    ws, send = _attach()
    try:
        send("Network.enable")
        return _has_auth_cookie(send)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _collect_x_cookies(send) -> list:
    """Read the live x.com/twitter.com cookies (CDP shape) from the managed
    Chrome. Returns [] if none. Shared by the mirror + server-store writers."""
    send("Network.enable")
    r = send("Network.getAllCookies")
    cks = r.get("result", {}).get("cookies", []) or []
    wanted = tuple(d.strip() for d in DOMAINS.split(",") if d.strip())
    return [c for c in cks if any(w in (c.get("domain") or "") for w in wanted)]


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_JSON = os.path.join(_REPO_ROOT, "config.json")
_HANDLE_PLACEHOLDERS = {"", "your-twitter-handle", "@your-twitter-handle"}


def _resolve_live_handle(send) -> "str | None":
    """Read the logged-in @handle from the LIVE x.com session via CDP DOM.

    resolve_handle() only reads config.json (which on a fresh install is the
    template placeholder), so it can't discover the real account. This reads the
    actual logged-in handle from the page so connect_x can persist it. Best
    effort: returns None on any failure and never raises into the connect flow.
    """
    js = r"""(function(){
      function fromHref(sel){var a=document.querySelector(sel);if(a){var h=a.getAttribute('href')||'';var m=h.match(/^\/([A-Za-z0-9_]{1,15})$/);if(m)return m[1];}return '';}
      var h=fromHref('a[data-testid="AppTabBar_Profile_Link"]');
      if(h)return h;
      var b=document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
      if(b){var m=(b.textContent||'').match(/@([A-Za-z0-9_]{1,15})/);if(m)return m[1];}
      return '';
    })()"""
    try:
        send("Page.enable")
        u = _current_url(send)
        if "x.com" not in u and "twitter.com" not in u:
            send("Page.navigate", {"url": "https://x.com/home"})
            time.sleep(3)
        for _ in range(8):
            r = send("Runtime.evaluate", {"expression": js, "returnByValue": True})
            v = (r.get("result", {}).get("result", {}) or {}).get("value", "") or ""
            v = v.strip().lstrip("@")
            if v:
                return v
            time.sleep(1)
    except Exception:
        return None
    return None


def _write_handle_to_config(handle: "str | None") -> bool:
    """Persist the discovered handle to config.json accounts.twitter.handle, but
    ONLY when the configured value is empty or the template placeholder, so we
    never clobber a handle the user set on purpose. Returns True if written.

    This is what makes account_resolver.resolve('twitter') return the REAL
    account, so our_account (attribution, own-reply skip, account-keyed ops) is
    correct instead of the poisonous 'your-twitter-handle' default. (2026-06-02)
    """
    if not handle:
        return False
    try:
        with open(_CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return False
    accounts = cfg.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        return False
    tw = accounts.setdefault("twitter", {})
    if not isinstance(tw, dict):
        return False
    cur = (tw.get("handle") or "").strip()
    if cur.lower() not in _HANDLE_PLACEHOLDERS:
        return False  # a real handle is already set; do not overwrite
    tw["handle"] = "@" + handle.lstrip("@")
    try:
        with open(_CONFIG_JSON, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        return True
    except Exception:
        return False


def _persist_session() -> None:
    """Persist the validated live X session for auto-restore after ANY logout
    (hard kill, crash, keychain re-lock wiping Chrome's Cookies DB, or AppMaker
    VM reseed). One CDP attach feeds two sinks:

      1. LOCAL 0600 mirror (twitter_cookie_mirror) — ALWAYS written. This is the
         keychain-independent durability layer that fixes Gap B on a persistent
         machine: restore_twitter_session.py re-injects from it on the next cycle
         preflight even after Chrome wiped its own encrypted store.
      2. Server-side session store (POST /api/v1/twitter/session-cookies) —
         best-effort. Enables VM auto-restore where the profile is reseeded
         hourly. No-op on a persistent machine with no social_accounts row.

    Non-fatal end-to-end: the local session is already valid; this only enables
    future auto-recovery, so nothing here may abort connect_x."""
    try:
        ws, send = _attach()
    except Exception:
        return
    try:
        cookies = _collect_x_cookies(send)
    except Exception:
        cookies = []
    finally:
        try:
            ws.close()
        except Exception:
            pass
    if not cookies:
        return

    # Prefer the LIVE logged-in handle so a fresh install records the real
    # account instead of the config.json placeholder; persist it so the cycle's
    # account_resolver (our_account) is correct. Fall back to the configured
    # handle. All best-effort: never abort connect_x.
    handle = _resolve_live_handle(send)
    if handle and _write_handle_to_config(handle):
        print(f"setup_twitter_auth: recorded live X handle @{handle} in config.json "
              "(accounts.twitter.handle); attribution + own-reply dedup now scoped "
              "to the real account", file=sys.stderr)
    if not handle and resolve_handle is not None:
        try:
            handle = resolve_handle()
        except Exception:
            handle = None

    # 1. Local mirror — always, keychain-independent.
    if twitter_cookie_mirror is not None:
        try:
            n = twitter_cookie_mirror.save_cookies(cookies, handle=handle)
            print(f"setup_twitter_auth: mirrored {n} x.com cookies to "
                  f"{twitter_cookie_mirror.MIRROR_PATH} (survives keychain re-lock "
                  "/ Cookies-DB wipe on relaunch)", file=sys.stderr)
        except Exception as e:
            print(f"setup_twitter_auth: local mirror save skipped ({e})", file=sys.stderr)

    # 2. Server store — best-effort, only when a handle resolves.
    if api_post is not None and handle:
        try:
            api_post("/api/v1/twitter/session-cookies", {"handle": handle, "cookies": cookies})
            print(f"setup_twitter_auth: saved {len(cookies)} session cookies for @{handle} "
                  "(server auto-restore enabled)", file=sys.stderr)
        # api_post raises SystemExit (BaseException, NOT Exception) on a 4xx/5xx —
        # e.g. "no social_accounts row" on a persistent machine that never
        # registered this handle. Best-effort: must never abort connect_x.
        except (Exception, SystemExit) as e:
            print(f"setup_twitter_auth: session-store save skipped ({e})", file=sys.stderr)


def _show_window_and_open_login() -> bool:
    """Make the managed Chrome window VISIBLE + focused and land it on the X login
    page, so the user can sign in by hand (the manual-login fallback).

    Why this is needed: the cron pipeline parks this same Chrome OFF-SCREEN
    (BH_WINDOW_POS 3042,-1032, a multi-monitor placement). If that window is
    already up when the user runs connect_x, ensure_chrome() short-circuits and
    the user would have an invisible window with nothing to log into. This mirrors
    s4l-plugin's bringToFront() discipline: put a real, focused login screen in
    front of the user. Returns True if we got the page onto x.com/login.
    """
    try:
        ws, send = _attach()
    except Exception:
        return False
    try:
        # Pull the window on-screen, normal state (undo any off-screen parking).
        try:
            win = send("Browser.getWindowForTarget")
            win_id = (win.get("result", {}) or {}).get("windowId")
            if win_id is not None:
                # Two steps: a minimized/parked window must be set normal before
                # its bounds will stick (macOS clamps otherwise).
                send("Browser.setWindowBounds",
                     {"windowId": win_id, "bounds": {"windowState": "normal"}})
                send("Browser.setWindowBounds",
                     {"windowId": win_id,
                      "bounds": {"left": 80, "top": 80, "width": 1100, "height": 900}})
        except Exception:
            pass
        # Land on the real login flow and focus the tab.
        try:
            send("Page.enable")
            send("Page.navigate", {"url": "https://x.com/i/flow/login"})
            send("Page.bringToFront")
            return True
        except Exception:
            return False
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _poll_for_login(timeout: float = 90.0, interval: float = 2.0) -> bool:
    """Wait for the user to finish a MANUAL login, up to `timeout` seconds.

    Why this exists: connect_x used to return `needs_login` the instant it found
    no session, then relied on the agent driving the setup wizard to re-check
    only after the human had logged in. The agent loops faster than a person can
    type a password + 2FA, so it would re-run, still see `connected: false`, and
    misreport the handle as missing (a detection race, not a bad write).

    By owning the wait HERE, the tool blocks until the auth cookie actually
    appears (or the bounded window elapses), so no caller can race ahead of the
    human. Read-only: polls the auth_token cookie without navigating, so it never
    disrupts the login flow the user is in the middle of. Stays well under the
    MCP call timeout. Returns True once logged in, False if the window elapsed.
    """
    try:
        ws, send = _attach()
    except Exception:
        return False
    try:
        send("Network.enable")
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                if _has_auth_cookie(send):
                    return True
            except Exception:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(max(0.5, interval))
    finally:
        try:
            ws.close()
        except Exception:
            pass


# --- Source detection (no keychain, no decryption) --------------------------
# These let us prompt the OS keychain for exactly ONE browser (the one that
# actually holds an x.com session) instead of blindly walking all four, and
# power the panel's "import from" dropdown. They read the Cookies SQLite for the
# PRESENCE of an auth_token ROW; the value stays encrypted, so no Safe Storage
# prompt is shown.

def _profile_has_x_session(profile) -> bool:
    """True if `profile`'s Cookies DB has an x.com/twitter.com auth_token row.

    Filesystem + SQLite only — never reads the keychain or decrypts a value, so
    it triggers NO macOS Safe Storage prompt. Used to pick the right import
    source and to flag browsers in the dropdown."""
    if _cbc is None:
        return False
    cookies_path = profile.path / "Cookies"
    if not cookies_path.exists():
        nested = profile.path / "Network" / "Cookies"
        cookies_path = nested if nested.exists() else cookies_path
    if not cookies_path.exists():
        return False
    tmp = _cbc.copy_db(cookies_path)
    if tmp is None:
        return False
    import shutil
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM cookies WHERE name='auth_token' "
                "AND (host_key LIKE '%x.com' OR host_key LIKE '%twitter.com') LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def _list_sources() -> list[dict]:
    """Every installed Chromium-family profile with an `x_session` flag.

    Sorted Chrome-first, then sessions-found-first. Pure filesystem detection;
    no keychain prompt."""
    if _cbc is None:
        return []
    out: list[dict] = []
    for p in _cbc.detect_browsers():
        out.append({
            "spec": f"{p.browser}:{p.name}",
            "browser": p.browser,
            "profile": p.name,
            "label": f"{p.browser.capitalize()} \u2014 {p.name}",
            "x_session": _profile_has_x_session(p),
        })
    out.sort(key=lambda s: (s["browser"] != "chrome", not s["x_session"]))
    return out


def _auto_pick_sources() -> list[str]:
    """Default import order when the user didn't pick a browser. Prefer the
    browser(s) that actually have an x.com session so the keychain prompts for
    exactly the right one(s); fall back to Chrome. This is what replaces the old
    blind walk over all four browsers (which fired a keychain prompt per
    installed browser)."""
    srcs = _list_sources()
    with_session = [s["spec"] for s in srcs if s["x_session"]]
    if with_session:
        return with_session
    return ["chrome:Default"]


def cmd_detect_sources(args) -> dict:
    """List browsers/profiles the X session can be imported from (for the panel
    dropdown). Read-only, no keychain prompt."""
    sources = _list_sources()
    recommended = next((s["spec"] for s in sources if s["x_session"]), None)
    if not recommended:
        recommended = next((s["spec"] for s in sources if s["spec"] == "chrome:Default"),
                           sources[0]["spec"] if sources else "chrome:Default")
    return {"ok": True, "sources": sources, "recommended": recommended}


# --- Cookie import from the user's everyday browser -------------------------

def _import_from(source: str) -> dict:
    """Copy x.com/twitter.com cookies from `source` into the managed Chrome.

    Prefers the vendored copy_browser_cookies.py (ships with this repo, runs
    under the same interpreter that is already executing this script, so its
    deps are guaranteed present). Falls back to the legacy ai-browser-profile
    venv only on a dev box where the vendored script is absent.

    Returns {ok, returncode, stdout, stderr}. Cookie values are never surfaced;
    the copier prints counts only.
    """
    if VENDORED_COOKIE_SCRIPT.exists():
        cmd = [
            sys.executable, str(VENDORED_COOKIE_SCRIPT), "copy",
            "--from", source, "--to", CDP, "--domains", DOMAINS,
        ]
        cwd = str(VENDORED_COOKIE_SCRIPT.parent)
    elif ABP_PYTHON.exists():
        cmd = [
            str(ABP_PYTHON), "-m", "ai_browser_profile.cookies", "copy",
            "--from", source, "--to", CDP, "--domains", DOMAINS,
        ]
        cwd = str(Path.home() / "ai-browser-profile")
    else:
        return {
            "ok": False,
            "error": "no cookie copier available "
            f"(vendored script missing at {VENDORED_COOKIE_SCRIPT} and "
            f"ai-browser-profile venv not found at {ABP_PYTHON})",
        }
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"cookie copy from {source} timed out after 60s"}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


# --- Headless / Keychain pre-flight (#3 + #4, added 2026-06-02) -------------
# macOS Keychain access for Chrome's Safe Storage is GUI-session-gated. Calls
# from SSH-invoked processes (cron, ansible, the macstadium test runner, etc.)
# silently get errSecAuthFailed because there's no GUI to render an auth
# prompt to. Without these helpers, copy_browser_cookies.py fails with a
# generic "access denied", setup_twitter_auth re-classifies as needs_login,
# and the user sees "log in manually" when the actual cause is "your process
# can't read the OS keychain." This block detects the headless case up front
# AND classifies the import error so the user-facing message is accurate.

def _is_headless() -> bool:
    """True when running without a GUI/interactive session — the case where
    Keychain Safe Storage reads will silently deny without a prompt."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"):
        return True
    try:
        if not sys.stdin.isatty():
            return True
    except Exception:
        pass
    return False


def _keychain_safe_storage_ok(browser_label: str = "Chrome") -> tuple[bool, str]:
    """Probe whether the OS keychain entry for `<browser_label> Safe Storage`
    is readable by THIS process. Returns (ok, detail_for_log)."""
    svc = f"{browser_label} Safe Storage"
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", svc, "-a", browser_label, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"security probe failed: {e}"
    if r.returncode == 0:
        return True, "accessible"
    err_tail = (r.stderr or "").strip().splitlines()
    return False, (err_tail[-1] if err_tail else f"exit {r.returncode}")


def _classify_import_error(detail: str | None) -> str:
    """Map a copy_browser_cookies.py error string to a structured type so the
    upper layers (connect_x, the user) can show a precise remediation instead
    of a generic 'needs_login'."""
    if not detail:
        return "unknown"
    d = detail.lower()
    # Keychain access issues — most common on headless runs.
    if ("user interaction is not allowed" in d) or ("interaction is not allowed" in d):
        return "keychain_locked"
    if ("access denied" in d) or ("errsecauth" in d) or ("-25293" in d):
        return "keychain_acl_denied"
    if ("not be found in the keychain" in d) or ("errsecitemnotfound" in d):
        return "keychain_entry_missing"
    # Source profile / browser mapping
    if ("no profile" in d) or ("available" in d and "profiles" in d):
        return "source_profile_not_found"
    # CDP injection
    if ("websocket" in d) or ("connection refused" in d) or ("port" in d and "9555" in d):
        return "cdp_inject_failed"
    return "unknown"


def _cookies_db_path() -> Path | None:
    """Resolve the harness profile's on-disk Cookies SQLite. Newer Chrome nests
    it under Default/Network/; older builds keep it at Default/. Returns whichever
    exists (most-recently-modified wins if both linger), or None."""
    candidates = [
        PROFILE_DIR / "Default" / "Network" / "Cookies",
        PROFILE_DIR / "Default" / "Cookies",
    ]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


def _count_x_cookies_on_disk() -> int:
    """Count x.com/twitter.com rows committed to the on-disk Cookies SQLite.

    Reads a temp COPY of the DB (+ -wal/-shm) so an in-flight write by the live
    Chrome can't lock us out, and opens it read-write on the copy so WAL-resident
    rows are visible (a read-only open would miss not-yet-checkpointed writes —
    exactly the rows we are polling for). Returns the count, or -1 if the DB is
    missing/unreadable."""
    db = _cookies_db_path()
    if not db:
        return -1
    import shutil
    import sqlite3
    import tempfile
    tmpdir = None
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="saps_flushchk_"))
        dst = tmpdir / "Cookies"
        shutil.copy2(db, dst)
        for suffix in ("-wal", "-shm"):
            w = db.parent / (db.name + suffix)
            if w.exists():
                shutil.copy2(w, tmpdir / ("Cookies" + suffix))
        conn = sqlite3.connect(str(dst))
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM cookies "
                "WHERE host_key LIKE '%x.com' OR host_key LIKE '%twitter.com'"
            ).fetchone()[0]
        finally:
            conn.close()
        return int(n)
    except Exception:
        return -1
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _force_cookie_flush() -> tuple[bool, str]:
    """Flush Chrome's in-memory cookie store to disk via CDP Browser.close, then
    VERIFY the x.com cookies actually landed in the on-disk SQLite before
    returning (Gap A, 2026-06-02).

    The bug this fixes: Browser.close acks immediately, but Chrome commits the
    CookieMonster -> SQLite write ASYNCHRONOUSLY (~0.5-5s under load). The old
    code treated the RPC ack as proof of persistence and reported
    flushed_to_disk=true while the disk was still empty, so a doctor run or a
    SIGKILL in that window saw zero cookies. We now poll the on-disk row count
    until the flush is observably durable (or a timeout proves it isn't).

    Returns (ok, detail). ok=True only when x.com rows are confirmed on disk."""
    bh = Path.home() / ".local" / "bin" / "browser-harness"
    if not bh.exists():
        return False, f"browser-harness CLI missing at {bh}"
    before = _count_x_cookies_on_disk()
    env = os.environ.copy()
    env["BU_CDP_URL"] = CDP
    env.setdefault("BU_NAME", "twitter-harness")
    env["PATH"] = f"{Path.home()}/.local/bin:" + env.get("PATH", "")
    try:
        r = subprocess.run(
            [str(bh)],
            input="cdp('Browser.close')\n",
            env=env, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"browser-harness invocation failed: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]

    # Poll the disk for the async commit to land. Accept as soon as we observe
    # x.com rows on disk (and, if we had a baseline, that it didn't regress).
    deadline = time.time() + 8.0
    last = before
    while time.time() < deadline:
        n = _count_x_cookies_on_disk()
        if n > 0 and (before <= 0 or n >= before):
            return True, f"verified {n} x.com cookies committed to on-disk SQLite"
        last = n
        time.sleep(0.5)
    if last > 0:
        return True, f"verified {last} x.com cookies on disk (slow flush)"
    return False, (
        f"Browser.close issued but on-disk x.com cookie count is {last} after 8s "
        "(flush not confirmed; relying on the local cookie mirror for durability)"
    )


# --- Commands ---------------------------------------------------------------

def _configured_handle() -> "str | None":
    """The handle persisted in config.json (accounts.twitter.handle), or None if
    it's empty / still the template placeholder. Used to surface a `handle` on
    status WITHOUT navigating the live browser. None means UNKNOWN, never that a
    real handle is missing."""
    try:
        with open(_CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        h = ((cfg.get("accounts") or {}).get("twitter") or {}).get("handle") or ""
    except Exception:
        return None
    h = h.strip()
    if h.lower() in _HANDLE_PLACEHOLDERS:
        return None
    return "@" + h.lstrip("@")


def cmd_status(args) -> dict:
    if not ensure_chrome(launch=False):
        return {
            "ok": True,
            "connected": False,
            "state": "browser_not_running",
            # null = unknown (browser down), NOT a missing/wrong handle.
            "handle": None,
            "note": "The autoposter's X browser isn't running yet. Run connect_x to "
            "start it and check/import your session.",
            "cdp": CDP,
        }
    try:
        valid = _has_session_quick()
    except Exception as e:
        return {"ok": False, "connected": False, "state": "error",
                "handle": None, "error": str(e), "cdp": CDP}
    return {
        "ok": True,
        "connected": valid,
        "state": "connected" if valid else "logged_out",
        # Only report a handle when a session exists; logged_out -> null (unknown,
        # not missing). Callers must not treat a logged_out result as a reason to
        # ask for / overwrite the handle.
        "handle": _configured_handle() if valid else None,
        "cdp": CDP,
    }


def cmd_connect(args) -> dict:
    if not ensure_chrome(launch=not args.no_launch):
        return {
            "ok": False,
            "connected": False,
            "state": "browser_launch_failed",
            "error": "Could not start the managed Chrome (no Chrome/Chromium found, "
            "or it failed to bind the debug port). Set BH_CHROME_BIN to your Chrome path.",
            "cdp": CDP,
        }

    # 1. Already logged in? Nothing to import.
    try:
        if _is_session_valid():
            _persist_session()
            return {
                "ok": True,
                "connected": True,
                "state": "connected",
                "source": "existing_session",
                "note": "X is already connected in the autoposter browser; nothing imported.",
                "cdp": CDP,
            }
    except Exception as e:
        return {"ok": False, "connected": False, "state": "error", "error": str(e), "cdp": CDP}

    # 1b. Headless + Keychain pre-flight (#3 + #4, added 2026-06-02).
    # On macOS, copy_browser_cookies.py needs to read the per-browser Safe
    # Storage entry from the OS keychain. SSH-invoked processes get
    # errSecAuthFailed silently — no prompt, no warning. We probe up front so
    # the user sees "your keychain is locked / run unlock-keychain" instead of
    # the misleading "log in manually" cascade.
    headless = _is_headless()
    if headless:
        # Probe with the first source's likely browser label. We don't know
        # which source will succeed yet, so probe Chrome (the autoposter
        # default); if that's denied, all the AUTO_SOURCES will be too.
        kc_ok, kc_detail = _keychain_safe_storage_ok("Chrome")
        if not kc_ok:
            return {
                "ok": True,
                "connected": False,
                "state": "keychain_locked",
                "error_type": "keychain_locked",
                "headless": True,
                "keychain_detail": kc_detail,
                "note": (
                    "Cookie import requires reading Chrome's Safe Storage from the macOS "
                    "Keychain, but this process can't access it (probably running over SSH "
                    "or another headless context). No GUI prompt is shown for this — macOS "
                    "denies access silently. To fix, run this once in the same session:\n"
                    "  security unlock-keychain ~/Library/Keychains/login.keychain-db\n"
                    "Then re-run connect_x. If you're on the autoposter machine via SSH, you "
                    "may also need to run it before every fresh shell, or persist with "
                    "`security set-keychain-settings -lut 0`."
                ),
                "remediation_cmd": "security unlock-keychain ~/Library/Keychains/login.keychain-db",
                "cdp": CDP,
            }

    # 2. Import from the user's everyday browser.
    #    - explicit --source X        -> just that one (one keychain prompt)
    #    - --source all               -> the full chrome/arc/brave/edge sweep (legacy)
    #    - no --source (the default)  -> auto-pick the browser(s) that ACTUALLY
    #      hold an x.com session, so we prompt the keychain for exactly the right
    #      one instead of blindly walking all four and prompting per browser.
    if args.source == "all":
        sources = AUTO_SOURCES
    elif args.source:
        sources = [args.source]
    else:
        sources = _auto_pick_sources()
    attempts = []
    for src in sources:
        res = _import_from(src)
        copied = res.get("stdout", "")
        detail = copied or res.get("error") or res.get("stderr")
        # #3: classify the error so the caller doesn't see string soup.
        error_type = None if res.get("ok") else _classify_import_error(detail)
        attempts.append({
            "source": src,
            "ok": res.get("ok"),
            "detail": detail,
            "error_type": error_type,
        })
        if not res.get("ok"):
            continue
        # 3. Re-validate after this source.
        try:
            if _is_session_valid():
                _persist_session()
                # #2: force a cookie-store flush via CDP Browser.close so the
                # imported session survives any subsequent SIGKILL (e.g. the
                # autoposter cron stopping Chrome with no grace window). Empty
                # result on this build is success — Browser.close triggers the
                # flush synchronously but doesn't actually terminate Chrome.
                flush_ok, flush_detail = _force_cookie_flush()
                mirror_count = (
                    twitter_cookie_mirror.load_meta().get("count")
                    if twitter_cookie_mirror is not None else None
                )
                return {
                    "ok": True,
                    "connected": True,
                    "state": "imported",
                    "source": src,
                    "attempts": attempts,
                    "flushed_to_disk": flush_ok,
                    "flush_detail": flush_detail,
                    "mirrored_cookies": mirror_count,
                    "note": f"Imported your X session from {src} into the autoposter browser. "
                            + ("Cookies verified on disk AND mirrored locally; "
                               if flush_ok else
                               "Chrome's encrypted store didn't confirm the flush, but ")
                            + (f"{mirror_count} cookies are saved to a keychain-independent "
                               "mirror, so the cycle preflight auto-restores the session even if "
                               "Chrome re-launches logged out."
                               if mirror_count else
                               "the session is live in the running browser."),
                    "cdp": CDP,
                }
        except Exception:
            pass

    # 4. Could not establish a valid session automatically -> manual login.
    #    Put a real, focused X login screen in front of the user (the cron
    #    pipeline may have parked this window off-screen) and tell them to sign
    #    in by hand, then re-run connect_x. We never ask for their password and
    #    never hand-decrypt cookies; they log into their own browser themselves.
    shown = _show_window_and_open_login()

    # Own the wait: block here until the user finishes the manual login (or the
    # bounded window elapses) instead of returning `needs_login` instantly and
    # letting the caller re-check faster than a human can type a password + 2FA.
    # That race is what made setup misreport the handle as "missing." If the
    # cookie appears, fall through to the same connected/persist/handle path the
    # auto-import success branch uses.
    login_wait = getattr(args, "login_wait", 90.0)
    if login_wait and login_wait > 0 and _poll_for_login(timeout=login_wait):
        try:
            if _is_session_valid():
                _persist_session()
                flush_ok, flush_detail = _force_cookie_flush()
                return {
                    "ok": True,
                    "connected": True,
                    "state": "connected",
                    "source": "manual_login",
                    "attempts": attempts,
                    "flushed_to_disk": flush_ok,
                    "flush_detail": flush_detail,
                    "note": "You logged in manually; the autoposter detected the live X "
                            "session and saved it to its own profile.",
                    "cdp": CDP,
                }
        except Exception:
            pass

    note = (
        "A Chrome window for the autoposter is open at the X login page"
        + ("" if shown else " (if you don't see it, look for a 'Google Chrome' window)")
        + " and you are NOT logged in yet. Log in there yourself — username, password, "
        "and 2FA if prompted — in that window. When your X home timeline shows, ask me "
        "to confirm and I'll re-check (run connect_x again). The session is saved to the "
        "autoposter's own profile, so this is a one-time step. "
        "(Auto-import tried: " + ", ".join(sources) + ".)"
    )
    # If every attempt classified to the same root cause, surface it so the
    # caller doesn't keep telling the user "log in manually" when really the
    # keychain is locked / no source profile exists / CDP isn't reachable.
    distinct_error_types = {a.get("error_type") for a in attempts if a.get("error_type")}
    rolled_up_error_type = (
        next(iter(distinct_error_types)) if len(distinct_error_types) == 1 else None
    )
    return {
        "ok": True,
        "connected": False,
        "state": "needs_login",
        # null = the handle is UNKNOWN because no session exists yet, NOT that a
        # configured handle is missing/wrong. Callers must never treat a
        # logged-out result as a handle-remediation trigger.
        "handle": None,
        "error_type": rolled_up_error_type,
        "attempts": attempts,
        "login_window_opened": shown,
        "note": note,
        "profile_dir": str(PROFILE_DIR),
        "cdp": CDP,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Twitter/X session bootstrap for MCP setup.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Report whether the managed X session is valid.")
    sub.add_parser("detect-sources",
                   help="List browsers/profiles to import the X session from "
                        "(JSON, for the panel dropdown). No keychain prompt.")
    c = sub.add_parser("connect", help="Ensure browser + import/validate the X session.")
    c.add_argument("--source", default=None,
                   help="Browser profile to import from (e.g. chrome:Default, arc:Default), "
                        "or 'all' for the full chrome/arc/brave/edge sweep. Default: "
                        "auto-pick the browser that actually holds an x.com session "
                        "(one keychain prompt for the right browser).")
    c.add_argument("--no-launch", action="store_true",
                   help="Do not launch Chrome if it's down (probe only).")
    c.add_argument("--login-wait", type=float, default=90.0,
                   help="Seconds to wait for a MANUAL login to complete before "
                        "returning needs_login (default 90; 0 disables the wait). "
                        "Prevents the detection race that misreports the handle as missing.")
    args = ap.parse_args()

    if args.cmd == "detect-sources":
        # Pure filesystem; never needs CDP/websocket.
        out = cmd_detect_sources(args)
    elif _WEBSOCKET_IMPORT_ERROR is not None:
        # status/connect attach to Chrome over CDP — websocket-client is required.
        out = {"ok": False, "state": "error", "error": _WEBSOCKET_IMPORT_ERROR}
    elif args.cmd == "status":
        out = cmd_status(args)
    else:
        out = cmd_connect(args)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
