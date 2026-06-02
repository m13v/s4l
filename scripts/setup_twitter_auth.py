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

try:
    from websocket import create_connection  # websocket-client
except ImportError:
    print(
        json.dumps(
            {
                "ok": False,
                "state": "error",
                "error": "websocket-client not installed (needed for CDP). "
                "pip install websocket-client",
            }
        )
    )
    sys.exit(0)

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


def _save_session_to_store() -> None:
    """Best-effort: persist the live x.com cookies to the server-side session
    store (social_accounts.session_cookies, via POST /api/v1/twitter/session-cookies)
    so restore_twitter_session.py can auto-re-inject them on the next preflight
    after ANY logout — hard kill, crash, or AppMaker VM reseed. Non-fatal: the
    local session is already valid; this only enables future auto-recovery.
    Without it the restore rail has nothing to read and every logout needs a
    manual connect_x."""
    if api_post is None or resolve_handle is None:
        return
    try:
        handle = resolve_handle()
    except Exception:
        handle = None
    if not handle:
        return
    try:
        ws, send = _attach()
    except Exception:
        return
    try:
        send("Network.enable")
        r = send("Network.getAllCookies")
        cks = r.get("result", {}).get("cookies", []) or []
        wanted = tuple(d.strip() for d in DOMAINS.split(",") if d.strip())
        cookies = [c for c in cks if any(w in (c.get("domain") or "") for w in wanted)]
        if not cookies:
            return
        api_post("/api/v1/twitter/session-cookies", {"handle": handle, "cookies": cookies})
        print(f"setup_twitter_auth: saved {len(cookies)} session cookies for @{handle} "
              "(auto-restore enabled)", file=sys.stderr)
    # api_post raises SystemExit (BaseException, NOT Exception) on a 4xx/5xx —
    # e.g. "no social_accounts row" on a persistent machine that never registered
    # this handle. The save is best-effort and must never abort connect_x, so
    # catch SystemExit too.
    except (Exception, SystemExit) as e:
        print(f"setup_twitter_auth: session-store save skipped ({e})", file=sys.stderr)
    finally:
        try:
            ws.close()
        except Exception:
            pass


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


# --- Commands ---------------------------------------------------------------

def cmd_status(args) -> dict:
    if not ensure_chrome(launch=False):
        return {
            "ok": True,
            "connected": False,
            "state": "browser_not_running",
            "note": "The autoposter's X browser isn't running yet. Run connect_x to "
            "start it and check/import your session.",
            "cdp": CDP,
        }
    try:
        valid = _has_session_quick()
    except Exception as e:
        return {"ok": False, "connected": False, "state": "error", "error": str(e), "cdp": CDP}
    return {
        "ok": True,
        "connected": valid,
        "state": "connected" if valid else "logged_out",
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
            _save_session_to_store()
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

    # 2. Import from the user's everyday browser.
    sources = [args.source] if args.source else AUTO_SOURCES
    attempts = []
    for src in sources:
        res = _import_from(src)
        copied = res.get("stdout", "")
        attempts.append({"source": src, "ok": res.get("ok"), "detail": copied or res.get("error") or res.get("stderr")})
        if not res.get("ok"):
            continue
        # 3. Re-validate after this source.
        try:
            if _is_session_valid():
                _save_session_to_store()
                return {
                    "ok": True,
                    "connected": True,
                    "state": "imported",
                    "source": src,
                    "attempts": attempts,
                    "note": f"Imported your X session from {src} into the autoposter browser.",
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
    note = (
        "A Chrome window for the autoposter is open at the X login page"
        + ("" if shown else " (if you don't see it, look for a 'Google Chrome' window)")
        + " and you are NOT logged in yet. Log in there yourself — username, password, "
        "and 2FA if prompted — in that window. When your X home timeline shows, ask me "
        "to confirm and I'll re-check (run connect_x again). The session is saved to the "
        "autoposter's own profile, so this is a one-time step. "
        "(Auto-import tried: " + ", ".join(sources) + ".)"
    )
    return {
        "ok": True,
        "connected": False,
        "state": "needs_login",
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
    c = sub.add_parser("connect", help="Ensure browser + import/validate the X session.")
    c.add_argument("--source", default=None,
                   help="Browser profile to import from (e.g. chrome:Default, arc:Default). "
                        "Default: auto-detect chrome/arc/brave/edge.")
    c.add_argument("--no-launch", action="store_true",
                   help="Do not launch Chrome if it's down (probe only).")
    args = ap.parse_args()

    if args.cmd == "status":
        out = cmd_status(args)
    else:
        out = cmd_connect(args)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
