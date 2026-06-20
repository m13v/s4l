#!/usr/bin/env python3
"""Restore the harness Chrome's Twitter session from the durable cookie mirror.

This is the keychain-independent recovery path (Gap B). On a persistent Mac the
harness Chrome can come up logged out: a hard restart or a macOS keychain
re-lock can leave Chrome unable to decrypt its cookie store, so it wipes it to
an empty schema. The cycle preflight calls this to heal that automatically:

  1. Attach to the harness Chrome (TWITTER_CDP_URL, default 127.0.0.1:9555 —
     the Mac harness port; AppMaker VMs override it to :9222 via the env file).
  2. Navigate to x.com/home; if it redirects to /login, the session is gone.
  3. Load cookies from the local 0600 mirror written on every connect
     (keychain-independent).
  4. Inject them via CDP Network.setCookies and reload.
  5. Verify we land on /home (logged in).

Idempotent + safe to run every cycle preflight: if already logged in, it's a
no-op. Exits 0 on logged-in (restored or already), 1 on failure (caller can
fall back to alerting for a manual re-login).

Run: python3 scripts/restore_twitter_session.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Local 0600 cookie mirror — the keychain-independent restore source (Gap B).
# It is the ONLY cookie source; the VM-era server store
# (/api/v1/twitter/session-cookies) was removed 2026-06-17. Stdlib-only;
# guarded so a path quirk never breaks the cycle preflight.
try:
    import twitter_cookie_mirror  # noqa: E402
except Exception:
    twitter_cookie_mirror = None

try:
    from websocket import create_connection
except ImportError:
    print("restore_twitter_session: websocket-client not installed", file=sys.stderr)
    sys.exit(1)

CDP = os.environ.get("TWITTER_CDP_URL", "http://127.0.0.1:9555").rstrip("/")


def _attach():
    targets = json.load(urllib.request.urlopen(f"{CDP}/json", timeout=10))
    page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        # create a tab if none
        new = json.load(urllib.request.urlopen(
            urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT"), timeout=10))
        page = new
    # suppress_origin: Chrome 111+ enforces CDP WebSocket origin checking and
    # rejects the handshake with 403 unless Chrome was launched with
    # --remote-allow-origins. The harness Chrome (twitter-backend.sh) is launched
    # without that flag, so we must suppress the Origin header (localhost CDP is
    # already privileged), matching setup_twitter_auth.py / copy_browser_cookies.py.
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


def _current_url(send):
    r = send("Runtime.evaluate", {"expression": "location.href", "returnByValue": True})
    return (r.get("result", {}).get("result", {}) or {}).get("value", "") or ""


def _has_auth_cookie(send):
    """The reliable logged-in signal: an auth_token cookie on x.com.
    URL heuristics are unreliable — x.com/ (root) is the logged-OUT landing,
    not a login URL, so a URL-only check false-positives."""
    r = send("Network.getAllCookies")
    cks = r.get("result", {}).get("cookies", []) or []
    return any(
        c.get("name") == "auth_token" and "x.com" in (c.get("domain") or "")
        for c in cks
    )


def _logged_in(send):
    send("Network.enable")
    if _has_auth_cookie(send):
        return True
    # No auth cookie in the current store — navigate to force x.com to set/clear
    # session cookies, then re-check.
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


def _inject(send, cookies) -> int:
    """Inject CDP-shaped cookies via Network.setCookie. Returns accepted count."""
    send("Network.enable")
    ok_count = 0
    for c in cookies:
        params = {k: c[k] for k in (
            "name", "value", "domain", "path", "secure", "httpOnly",
            "sameSite", "expires") if k in c and c[k] is not None}
        r = send("Network.setCookie", params)
        if r.get("result", {}).get("success", True):
            ok_count += 1
    return ok_count


def _stored_cookies():
    """Return (cookies, source) from the LOCAL 0600 mirror, or ([], None).

    The mirror is the only cookie source; the VM-era server store
    (/api/v1/twitter/session-cookies) was removed 2026-06-17."""
    if twitter_cookie_mirror is not None:
        try:
            mirrored = twitter_cookie_mirror.load_cookies()
        except Exception:
            mirrored = []
        if mirrored:
            return mirrored, f"local mirror ({twitter_cookie_mirror.MIRROR_PATH.name})"
    return [], None


def main():
    try:
        ws, send = _attach()
    except Exception as e:
        print(f"restore_twitter_session: cannot attach to {CDP}: {e}", file=sys.stderr)
        return 1

    try:
        if _logged_in(send):
            print("restore_twitter_session: already logged in; no-op")
            return 0

        cookies, source = _stored_cookies()
        if not cookies:
            print("restore_twitter_session: no stored cookies (local mirror empty); "
                  "manual connect_x required", file=sys.stderr)
            return 1

        print(f"restore_twitter_session: logged out, restoring from {source}...")
        ok_count = _inject(send, cookies)
        print(f"restore_twitter_session: injected {ok_count}/{len(cookies)} cookies")

        if _logged_in(send):
            print(f"restore_twitter_session: RESTORED session from {source}")
            return 0
        print("restore_twitter_session: injection done but still logged out "
              "(cookies may be expired); manual connect_x required", file=sys.stderr)
        return 1
    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
