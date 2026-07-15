#!/usr/bin/env python3
"""setup_reddit_auth.py - Reddit session bootstrap for the MCP setup flow.

Used by the social-autoposter MCP `project_config` tool (action=connect_reddit)
to give a user a logged-in Reddit session in the autoposter's managed
reddit-harness Chrome WITHOUT making them paste cookies or hand-edit anything.
Mirrors scripts/setup_twitter_auth.py, parameterized for Reddit.

It answers the three questions the setup flow needs:
  1. Do cookies already exist in the managed reddit-harness browser?
  2. Are they still valid?  (api/me.json returns a real, non-suspended account
     via a same-origin fetch from inside a reddit.com page)
  3. Does the user need to log in manually?  (import failed / no source)

How it works
------------
The Reddit pipeline rides a dedicated managed Chrome on CDP port 9557 with a
persistent profile at ~/.claude/browser-profiles/reddit-harness (the same
Chrome skill/lib/reddit-backend.sh launches for discovery + posting). This
helper:

  status  - probe that Chrome; if up, do a read-only reddit_session cookie
            check (cheap and poll-safe: never navigates the shared harness
            tab; the authoritative me.json validation lives in `connect`).
            If down, fall back to a read-only on-disk cookie check
            (connected_idle) so a saved session never reads back as
            "disconnected" just because the harness isn't running.
  connect - ensure that Chrome is running; if the reddit session is already
            valid, no-op; otherwise IMPORT reddit.com cookies (reddit_session,
            token_v2, and the rest of the reddit.com jar) from the user's
            everyday browser (Chrome/Arc/Brave/Edge, auto-detected) via the
            vendored copy_browser_cookies.py, then re-validate. If still
            logged out, open a visible login window (manual-login fallback)
            and wait for the user to sign in once.

Only reddit.com cookies are copied. No other site's session is touched, and
cookie VALUES are never printed.

On a valid session the result also carries account context so onboarding can
set expectations: account_age_days + comment_karma from me.json, plus a
`warning` string when the account is fresh/low-karma (AutoMod silently gates
those in most subreddits). The warning NEVER blocks the connect.

The username is returned to the caller; the MCP server persists it into
config accounts.reddit.username through its own config-write path (this
script deliberately does NOT write config.json).

Output: a single JSON object on stdout. Human-readable notes go to stderr.

CLI:
  python3 setup_reddit_auth.py status
  python3 setup_reddit_auth.py detect-sources
  python3 setup_reddit_auth.py connect [--source chrome:Default] [--no-launch]
                                       [--manual-login] [--login-wait 300]
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

# websocket-client is needed for CDP cookie polling (connect). Deferred error,
# same as setup_twitter_auth.py: `detect-sources` is pure filesystem.
try:
    from websocket import create_connection  # websocket-client
    _WEBSOCKET_IMPORT_ERROR = None
except ImportError:
    create_connection = None  # type: ignore[assignment]
    _WEBSOCKET_IMPORT_ERROR = (
        "websocket-client not installed (needed for CDP). pip install websocket-client"
    )

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Shared, platform-agnostic helpers from the X bootstrap (headless detection,
# keychain preflight, import-error classification). Reused, not duplicated, so
# a fix there reaches both platforms. Guarded: a missing module never breaks
# reddit setup, we just degrade to coarse error strings.
try:
    from setup_twitter_auth import (  # noqa: E402
        _classify_import_error,
        _is_headless,
        _keychain_safe_storage_ok,
    )
except Exception:  # pragma: no cover - defensive
    def _classify_import_error(detail):  # type: ignore[misc]
        return "unknown"

    def _is_headless():  # type: ignore[misc]
        return False

    def _keychain_safe_storage_ok(browser_label="Chrome"):  # type: ignore[misc]
        return True, "probe unavailable"

# Vendored cookie copier - stdlib-only browser/profile detection plus the
# actual keychain-decrypt + CDP-inject copy. Same module connect_x uses,
# parameterized here by --domains reddit.com.
try:
    import copy_browser_cookies as _cbc  # noqa: E402
except Exception:
    _cbc = None

# Same-origin me.json fetch through the logged-in harness page. This is the
# ONLY fetch path Reddit reliably 200s (bare urllib/curl 403s from residential
# IPs since 2026-05-28); it also yields to an active posting drain.
try:
    from reddit_browser_fetch import browser_get_json  # noqa: E402
except Exception:
    browser_get_json = None

# --- Config -----------------------------------------------------------------

# Same managed Chrome the reddit pipeline uses (skill/lib/reddit-backend.sh).
CDP = os.environ.get(
    "S4L_REDDIT_CDP_URL", os.environ.get("REDDIT_CDP_URL", "http://127.0.0.1:9557")
).rstrip("/")
PORT = int(CDP.rsplit(":", 1)[-1]) if CDP.rsplit(":", 1)[-1].isdigit() else 9557
PROFILE_DIR = Path.home() / ".claude" / "browser-profiles" / "reddit-harness"
PID_FILE = Path.home() / ".claude" / "browser-profiles" / "reddit-harness.chrome.pid"

DOMAINS = "reddit.com"
ME_JSON_URL = "https://www.reddit.com/api/me.json"

VENDORED_COOKIE_SCRIPT = Path(__file__).resolve().parent / "copy_browser_cookies.py"

# Expectation thresholds for the fresh-account warning (advisory only; most
# subreddits' AutoMod removes comments from young / low-karma accounts).
FRESH_ACCOUNT_MIN_AGE_DAYS = 30
FRESH_ACCOUNT_MIN_COMMENT_KARMA = 100

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_JSON = os.path.join(_REPO_ROOT, "config.json")
_USERNAME_PLACEHOLDERS = {"", "your-reddit-username", "u/your-reddit-username"}


# --- Chrome lifecycle (mirrors setup_twitter_auth.py) ------------------------

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


def _resolve_chrome_bin() -> "str | None":
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
    """Launch the managed reddit-harness Chrome on PORT, ON-SCREEN.

    On-screen (unlike the pipeline's off-screen parking) because during setup
    the user may need to see this window to log in. Cookies persist on disk,
    so later off-screen relaunches by the pipeline inherit the session.

    IMPORTANT flag parity: skill/lib/reddit-backend.sh launches this profile
    WITHOUT --password-store=basic / --use-mock-keychain (unlike the twitter
    harness). Chrome encrypts the cookie SQLite with whichever key the launch
    flags select; mixing keyed and mock-keychain launches on the SAME profile
    makes Chrome unable to decrypt its own cookies and silently drops the
    session. So this launcher must match the pipeline's flags exactly.
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
        "--disable-hang-monitor",
        "--disable-features=ChromeWhatsNewUI,CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
    ]
    is_linux = sys.platform.startswith("linux")
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if is_linux:
        cmd += ["--no-sandbox", "--disable-dev-shm-usage"]
        if not has_display:
            cmd += ["--headless=new", "--disable-gpu"]
    else:
        cmd += ["--window-position=80,80", "--window-size=1100,900"]
    cmd.append("about:blank")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # start_new_session: don't let a transient launchd caller's process-group
    # SIGKILL reap this Chrome on exit (same fix as the backends, 2026-07-12).
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        PID_FILE.write_text(str(proc.pid))
    except OSError:
        pass
    # 40s poll window, wider than the twitter helper's 15s: the FIRST reddit
    # connect creates the harness profile from scratch, and on a cold box that
    # first launch takes >15s to bind the debug port (observed on the QA box
    # 2026-07-15: ensure_chrome gave up at 15s, Chrome bound seconds later and
    # the flow misreported browser_launch_failed).
    for _ in range(40):
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


# --- CDP attach (cookie polling + window control) ----------------------------

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


def _has_reddit_session_cookie(send) -> bool:
    # reddit_session ONLY: token_v2 is an anonymous-session JWT Reddit sets for
    # every visitor (logged in or not), so keying on it false-positives the
    # moment the harness merely VISITS reddit.com (observed on the QA box
    # 2026-07-15). reddit_session exists only for a logged-in account.
    r = send("Network.getAllCookies")
    cks = r.get("result", {}).get("cookies", []) or []
    return any(
        c.get("name") == "reddit_session"
        and "reddit.com" in (c.get("domain") or "")
        for c in cks
    )


def _has_session_quick() -> bool:
    """Read-only: reddit_session cookie present in the live browser?
    Never navigates, so it's safe while a pipeline run is active. A revoked
    cookie can false-positive; the me.json validation is authoritative."""
    ws, send = _attach()
    try:
        send("Network.enable")
        return _has_reddit_session_cookie(send)
    finally:
        try:
            ws.close()
        except Exception:
            pass


# --- me.json validation -------------------------------------------------------

def _fetch_me() -> "dict | None":
    """Fetch api/me.json through the logged-in harness page (same-origin fetch,
    the only transport Reddit reliably 200s). Returns the parsed `data` dict on
    a logged-in session, {} when logged out, None on transport failure."""
    if browser_get_json is None:
        return None
    body, status = browser_get_json(ME_JSON_URL, cdp_url=CDP)
    if status != 200 or not body:
        return None
    try:
        j = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(j, dict):
        return None
    data = j.get("data")
    return data if isinstance(data, dict) else {}


def _account_fields(data: dict) -> dict:
    """Account context for onboarding expectations: username, age, karma, and a
    non-blocking warning for fresh/low-karma accounts (AutoMod gates those)."""
    name = (data.get("name") or "").strip()
    created = data.get("created_utc") or data.get("created")
    age_days = None
    try:
        if created:
            age_days = max(0, int((time.time() - float(created)) / 86400))
    except (TypeError, ValueError):
        age_days = None
    comment_karma = data.get("comment_karma")
    total_karma = data.get("total_karma", data.get("link_karma"))
    warning = None
    reasons = []
    if age_days is not None and age_days < FRESH_ACCOUNT_MIN_AGE_DAYS:
        reasons.append(f"the account is only {age_days} day(s) old")
    if isinstance(comment_karma, (int, float)) and comment_karma < FRESH_ACCOUNT_MIN_COMMENT_KARMA:
        reasons.append(f"it has {int(comment_karma)} comment karma")
    if reasons:
        warning = (
            "Heads up: " + " and ".join(reasons) + ". Most subreddits' AutoMod "
            "silently removes or holds comments from fresh, low-karma accounts, so "
            "early replies may not be visible to others. This does not block setup; "
            "expect slower traction until the account ages and earns karma "
            "(participating manually for a few weeks helps)."
        )
    return {
        "username": name or None,
        "account_age_days": age_days,
        "comment_karma": int(comment_karma) if isinstance(comment_karma, (int, float)) else None,
        "total_karma": int(total_karma) if isinstance(total_karma, (int, float)) else None,
        "suspended": bool(data.get("is_suspended")),
        "warning": warning,
    }


def _validate_session() -> "dict | None":
    """Authoritative check: me.json through the live harness. Returns the
    account-fields dict when logged in with a usable account, or None."""
    data = _fetch_me()
    if not data:  # None (transport fail) or {} (logged out)
        return None
    fields = _account_fields(data)
    if not fields["username"]:
        return None
    return fields


# --- On-disk session detection (no keychain, no decryption) -------------------

def _cookies_db_path() -> "Path | None":
    candidates = [
        PROFILE_DIR / "Default" / "Network" / "Cookies",
        PROFILE_DIR / "Default" / "Cookies",
    ]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


def _profile_dir_has_session() -> bool:
    """True when the HARNESS profile's on-disk Cookies SQLite has a
    reddit_session row (login cookie ONLY; token_v2 is set for anonymous
    visitors too). Presence-only (value stays encrypted), so no
    macOS Safe Storage prompt. Lets `status` report connected_idle while the
    harness Chrome is down (the pipeline relaunches it with the same profile)."""
    if _cbc is None:
        return False
    db = _cookies_db_path()
    if db is None:
        return False
    tmp = _cbc.copy_db(db)
    if tmp is None:
        return False
    import shutil
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM cookies WHERE name = 'reddit_session' "
                "AND host_key LIKE '%reddit.com' LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def _configured_username() -> "str | None":
    """accounts.reddit.username from config.json (read-only), or None if empty /
    placeholder. Reads through the same file account_resolver reads."""
    try:
        with open(_CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        u = ((cfg.get("accounts") or {}).get("reddit") or {}).get("username") or ""
    except Exception:
        return None
    u = u.strip()
    if u.lower() in _USERNAME_PLACEHOLDERS:
        return None
    if u.lower().startswith("u/"):
        u = u[2:]
    return u or None


# --- Source detection ---------------------------------------------------------

def _profile_has_reddit_session(profile) -> bool:
    """True if `profile`'s Cookies DB has a reddit.com session cookie row.
    Filesystem + SQLite only; no keychain prompt."""
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
                "SELECT 1 FROM cookies WHERE name = 'reddit_session' "
                "AND host_key LIKE '%reddit.com' LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def _list_sources() -> "list[dict]":
    if _cbc is None:
        return []
    out: "list[dict]" = []
    for p in _cbc.detect_browsers():
        out.append({
            "spec": f"{p.browser}:{p.name}",
            "browser": p.browser,
            "profile": p.name,
            "label": f"{p.browser.capitalize()} - {p.name}",
            "reddit_session": _profile_has_reddit_session(p),
        })
    out.sort(key=lambda s: (s["browser"] != "chrome", not s["reddit_session"]))
    return out


def cmd_detect_sources(args) -> dict:
    sources = _list_sources()
    recommended = next((s["spec"] for s in sources if s["reddit_session"]), None)
    if not recommended:
        recommended = next((s["spec"] for s in sources if s["spec"] == "chrome:Default"),
                           sources[0]["spec"] if sources else "chrome:Default")
    return {"ok": True, "sources": sources, "recommended": recommended}


# --- Cookie import -------------------------------------------------------------

def _import_from(source: str) -> dict:
    """Copy reddit.com cookies from `source` into the managed harness Chrome via
    the vendored copier. Values are never surfaced; the copier prints counts."""
    if not VENDORED_COOKIE_SCRIPT.exists():
        return {
            "ok": False,
            "error": f"vendored cookie copier missing at {VENDORED_COOKIE_SCRIPT}",
        }
    cmd = [
        sys.executable, str(VENDORED_COOKIE_SCRIPT), "copy",
        "--from", source, "--to", CDP, "--domains", DOMAINS,
    ]
    # Keychain auth dialogs need human time; same generous window as connect_x.
    _raw_to = os.environ.get("S4L_COOKIE_COPY_TIMEOUT", "600").strip()
    try:
        copy_timeout = float(_raw_to) if _raw_to else None
    except ValueError:
        copy_timeout = 600.0
    if copy_timeout is not None and copy_timeout <= 0:
        copy_timeout = None
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=copy_timeout,
            cwd=str(VENDORED_COOKIE_SCRIPT.parent),
        )
    except subprocess.TimeoutExpired:
        _to_label = f"{copy_timeout:g}s" if copy_timeout is not None else "no limit"
        return {"ok": False, "error": f"cookie copy from {source} timed out ({_to_label})"}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


# --- Manual login fallback ------------------------------------------------------

def _show_window_and_open_login() -> bool:
    """Make the harness Chrome window visible + focused on the Reddit login page
    so the user can sign in by hand. Mirrors the X manual-login discipline."""
    try:
        ws, send = _attach()
    except Exception:
        return False
    try:
        try:
            win = send("Browser.getWindowForTarget")
            win_id = (win.get("result", {}) or {}).get("windowId")
            if win_id is not None:
                send("Browser.setWindowBounds",
                     {"windowId": win_id, "bounds": {"windowState": "normal"}})
                send("Browser.setWindowBounds",
                     {"windowId": win_id,
                      "bounds": {"left": 80, "top": 80, "width": 1100, "height": 900}})
        except Exception:
            pass
        try:
            send("Page.enable")
            send("Page.navigate", {"url": "https://www.reddit.com/login/"})
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
    """Block until the user finishes a MANUAL login (session cookie appears) or
    the bounded window elapses. Read-only polling; never navigates, so it can't
    disrupt the login flow. Prevents the caller re-checking faster than a human
    can type a password + 2FA (the detection race connect_x had)."""
    try:
        ws, send = _attach()
    except Exception:
        return False
    try:
        send("Network.enable")
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                if _has_reddit_session_cookie(send):
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


# --- Commands -------------------------------------------------------------------

def _connected_result(state: str, fields: dict, source: "str | None" = None,
                      note: "str | None" = None, attempts: "list | None" = None) -> dict:
    out = {
        "ok": True,
        "connected": True,
        "state": state,
        "username": fields.get("username"),
        "account_age_days": fields.get("account_age_days"),
        "comment_karma": fields.get("comment_karma"),
        "total_karma": fields.get("total_karma"),
        "warning": fields.get("warning"),
        "cdp": CDP,
    }
    if source:
        out["source"] = source
    if note:
        out["note"] = note
    if attempts:
        out["attempts"] = attempts
    return out


def _posting_drain_active() -> bool:
    """True while a poster is mid-drain (fresh reddit-posting-active.json).
    status must not grab the shared harness tab out from under it, and a live
    drain is itself proof of a working session."""
    flag = os.path.join(
        os.path.expanduser(os.environ.get("S4L_STATE_DIR") or "~/.social-autoposter-mcp"),
        "reddit-posting-active.json",
    )
    try:
        return (time.time() - os.path.getmtime(flag)) < 120
    except OSError:
        return False


def cmd_status(args) -> dict:
    # An active posting drain owns the one shared harness tab; don't navigate it
    # (and don't sit in reddit_browser_fetch's 5-minute yield inside a status
    # probe). The drain posting right now IS a valid session.
    if _posting_drain_active():
        return {
            "ok": True,
            "connected": True,
            "state": "connected_idle",
            "username": _configured_username(),
            "note": "Reddit posting is actively draining right now (which requires a "
            "valid session); skipped the live me.json probe to leave the browser tab "
            "to the poster.",
            "cdp": CDP,
        }
    if not ensure_chrome(launch=False):
        # Harness down. The persistent profile IS the durability layer for
        # reddit (the pipeline relaunches this exact profile), so trust an
        # on-disk session cookie row instead of demanding a live browser.
        if _profile_dir_has_session():
            return {
                "ok": True,
                "connected": True,
                "state": "connected_idle",
                "username": _configured_username(),
                "note": "Reddit is connected (session saved in the harness profile). "
                "The reddit browser isn't running this moment; the next pipeline "
                "run relaunches it with the same profile.",
                "cdp": CDP,
            }
        return {
            "ok": True,
            "connected": False,
            "state": "browser_not_running",
            "username": None,
            "note": "The autoposter's reddit browser isn't running yet. Run "
            "connect_reddit to start it and check/import your session.",
            "cdp": CDP,
        }
    # Live browser is up: read-only reddit_session cookie check. Deliberately
    # NOT the me.json validation: status is polled by the snapshot/menubar and
    # the kicker gate, and a me.json fetch navigates the ONE shared harness tab
    # (fighting discovery/posting). The authoritative me.json validation runs
    # inside `connect` (already-valid short-circuit, post-import, post-login).
    # A revoked-but-present cookie can false-positive here; the next connect or
    # pipeline run surfaces that loudly.
    try:
        quick = _has_session_quick()
    except Exception as e:
        return {"ok": False, "connected": False, "state": "error",
                "username": None, "error": str(e), "cdp": CDP}
    if quick:
        return {
            "ok": True,
            "connected": True,
            "state": "connected",
            "username": _configured_username(),
            "cdp": CDP,
        }
    if _profile_dir_has_session():
        return {
            "ok": True,
            "connected": True,
            "state": "connected_idle",
            "username": _configured_username(),
            "note": "A reddit session exists in the harness profile on disk; the "
            "live browser has not loaded it into memory yet.",
            "cdp": CDP,
        }
    return {"ok": True, "connected": False, "state": "logged_out",
            "username": None, "cdp": CDP}


def cmd_connect(args) -> dict:
    if not ensure_chrome(launch=not args.no_launch):
        return {
            "ok": False,
            "connected": False,
            "state": "browser_launch_failed",
            "error": "Could not start the managed reddit Chrome (no Chrome/Chromium "
            "found, or it failed to bind the debug port). Set BH_CHROME_BIN to your "
            "Chrome path.",
            "cdp": CDP,
        }

    # 1. Already logged in? Nothing to import.
    fields = _validate_session()
    if fields is not None and fields.get("suspended"):
        return {
            "ok": True,
            "connected": False,
            "state": "suspended",
            "username": fields.get("username"),
            "note": "This reddit account is suspended; posting is not possible.",
            "cdp": CDP,
        }
    if fields is not None:
        return _connected_result(
            "connected", fields, source="existing_session",
            note="Reddit is already connected in the autoposter's reddit browser; "
                 "nothing imported.")

    # 1b. Choose the path (import vs manual login) before any keychain work.
    if args.source == "all":
        sources = ["chrome:Default", "arc:Default", "brave:Default", "edge:Default"]
        has_importable = True
    elif args.source:
        sources = [args.source]
        has_importable = True
    else:
        _srcs = _list_sources()
        _with_session = [s["spec"] for s in _srcs if s.get("reddit_session")]
        sources = _with_session or ["chrome:Default"]
        has_importable = bool(_with_session)

    manual_login = bool(getattr(args, "manual_login", False))
    will_import = has_importable and not manual_login

    # 1c. Headless keychain preflight, import path only (manual login reads no
    # keychain and must never be blocked by this).
    if will_import and _is_headless():
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
                    "Cookie import requires reading the browser's Safe Storage from "
                    "the macOS Keychain, but this process can't access it (probably "
                    "running over SSH or another headless context). Run this once in "
                    "the same session:\n"
                    "  security unlock-keychain ~/Library/Keychains/login.keychain-db\n"
                    "Then re-run connect_reddit. Or connect Reddit with manual login "
                    "instead; that signs in directly in the autoposter's own browser "
                    "and needs no keychain."
                ),
                "remediation_cmd": "security unlock-keychain ~/Library/Keychains/login.keychain-db",
                "cdp": CDP,
            }

    # 2. Import from the user's everyday browser.
    attempts = []
    for src in (sources if will_import else []):
        res = _import_from(src)
        copied = res.get("stdout", "")
        detail = copied or res.get("error") or res.get("stderr")
        error_type = None if res.get("ok") else _classify_import_error(detail)
        attempts.append({
            "source": src,
            "ok": res.get("ok"),
            "detail": detail,
            "error_type": error_type,
        })
        if not res.get("ok"):
            continue
        fields = _validate_session()
        if fields is not None and not fields.get("suspended"):
            return _connected_result(
                "imported", fields, source=src, attempts=attempts,
                note=f"Imported your Reddit session from {src} into the autoposter's "
                     "reddit browser. The session persists in the harness profile on "
                     "disk, so pipeline relaunches inherit it.")
        if fields is not None and fields.get("suspended"):
            return {
                "ok": True,
                "connected": False,
                "state": "suspended",
                "username": fields.get("username"),
                "attempts": attempts,
                "note": "The imported reddit account is suspended; posting is not possible.",
                "cdp": CDP,
            }

    # 3. Could not establish a valid session automatically.
    distinct_error_types = {a.get("error_type") for a in attempts if a.get("error_type")}
    rolled_up_error_type = (
        next(iter(distinct_error_types)) if len(distinct_error_types) == 1 else None
    )
    open_login = (
        manual_login
        or not will_import
        or rolled_up_error_type == "keychain_acl_denied"
    )

    shown = False
    if open_login:
        shown = _show_window_and_open_login()
        login_wait = getattr(args, "login_wait", 300.0)
        if login_wait and login_wait > 0 and _poll_for_login(timeout=login_wait):
            fields = _validate_session()
            if fields is not None and not fields.get("suspended"):
                return _connected_result(
                    "connected", fields, source="manual_login", attempts=attempts,
                    note="You logged in manually; the autoposter detected the live "
                         "reddit session in its own browser profile.")

    if rolled_up_error_type == "keychain_acl_denied":
        note = (
            "It looks like you clicked Deny (or Cancel) on the macOS Keychain prompt. "
            "To import your Reddit session automatically, the autoposter needs to read "
            "your browser's \"Safe Storage\" key from your Keychain. Re-run "
            "connect_reddit and click Allow (or Always Allow) and the import will "
            "finish on its own. If you'd rather not grant keychain access, there's "
            "already a Chrome window open at the Reddit login page"
            + ("" if shown else " (look for a 'Google Chrome' window)")
            + "; just log in there by hand and ask me to re-check. "
            "(Auto-import tried: " + ", ".join(sources) + ".)"
        )
        extra = {"remediation": "rerun_connect_reddit_and_click_allow"}
    elif open_login:
        _why = (
            "" if will_import
            else "No existing Reddit session was found in your everyday browser to import, so "
        )
        _tried = (" (Auto-import tried: " + ", ".join(sources) + ".)") if will_import else ""
        note = (
            _why
            + "a Chrome window for the autoposter is open at the Reddit login page"
            + ("" if shown else " (if you don't see it, look for a 'Google Chrome' window)")
            + " and you are NOT logged in yet. Log in there yourself (username, password, "
            "and 2FA if prompted) in that window. When your Reddit home feed shows, ask me "
            "to confirm and I'll re-check (run connect_reddit again). The session is saved "
            "to the autoposter's own profile, so this is a one-time step." + _tried
        )
        extra = {}
    else:
        note = (
            "Couldn't import a Reddit session automatically (auto-import tried: "
            + ", ".join(sources) + "). This usually means you're not logged into Reddit "
            "in your everyday browser, so there was no session to copy. I did NOT open "
            "a login window. If you want to sign in by hand, ask me to connect Reddit "
            "with manual login and I'll open a focused Reddit login page for you."
        )
        extra = {"manual_login_hint": "rerun_connect_reddit_with_manual_login"}
    return {
        "ok": True,
        "connected": False,
        "state": "needs_login",
        "username": None,
        "error_type": rolled_up_error_type,
        "attempts": attempts,
        "login_window_opened": shown,
        "note": note,
        "profile_dir": str(PROFILE_DIR),
        "cdp": CDP,
        **extra,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Reddit session bootstrap for MCP setup.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Report whether the managed reddit session is valid.")
    sub.add_parser("detect-sources",
                   help="List browsers/profiles to import the Reddit session from "
                        "(JSON). No keychain prompt.")
    c = sub.add_parser("connect", help="Ensure browser + import/validate the Reddit session.")
    c.add_argument("--source", default=None,
                   help="Browser profile to import from (e.g. chrome:Default, arc:Default), "
                        "or 'all' for the full sweep. Default: auto-pick the browser that "
                        "actually holds a reddit.com session.")
    c.add_argument("--no-launch", action="store_true",
                   help="Do not launch Chrome if it's down (probe only).")
    c.add_argument("--manual-login", action="store_true",
                   help="Explicitly opt into manual login: open a focused Reddit login "
                        "window and wait for the user to sign in by hand.")
    c.add_argument("--login-wait", type=float, default=300.0,
                   help="Seconds to wait for a MANUAL login to complete before "
                        "returning needs_login (default 300; 0 disables the wait).")
    args = ap.parse_args()

    if args.cmd == "detect-sources":
        out = cmd_detect_sources(args)
    elif _WEBSOCKET_IMPORT_ERROR is not None and args.cmd != "status":
        out = {"ok": False, "state": "error", "error": _WEBSOCKET_IMPORT_ERROR}
    elif args.cmd == "status":
        out = cmd_status(args)
    else:
        out = cmd_connect(args)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
