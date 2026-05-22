#!/usr/bin/env python3
"""Restore the Twitter browser session on a fresh AppMaker sandbox.

AppMaker VMs run on the E2B Hobby tier (1h sandbox TTL), so the sandbox is
substituted ~hourly and /root is reseeded from /etc/skel-root, wiping the
logged-in harness Chrome profile. This script makes that harmless:

  1. Attach to the harness Chrome (TWITTER_CDP_URL, default 127.0.0.1:9222).
  2. Navigate to x.com/home; if it redirects to /login, the session is gone.
  3. Fetch the stored cookies for this machine's handle from the HTTP API
     (GET /api/v1/twitter/session-cookies?handle=...), which reads
     social_accounts.session_cookies server-side (the VM has no DATABASE_URL).
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
from http_api import api_get  # noqa: E402
from twitter_account import resolve_handle  # noqa: E402

try:
    from websocket import create_connection
except ImportError:
    print("restore_twitter_session: websocket-client not installed", file=sys.stderr)
    sys.exit(1)

CDP = os.environ.get("TWITTER_CDP_URL", "http://127.0.0.1:9222").rstrip("/")


def _attach():
    targets = json.load(urllib.request.urlopen(f"{CDP}/json", timeout=10))
    page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        # create a tab if none
        new = json.load(urllib.request.urlopen(
            urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT"), timeout=10))
        page = new
    ws = create_connection(page["webSocketDebuggerUrl"], timeout=20)
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


def _logged_in(send):
    send("Page.enable")
    send("Page.navigate", {"url": "https://x.com/home"})
    # poll for settle
    for _ in range(20):
        time.sleep(1)
        u = _current_url(send)
        if "/login" in u or "/i/flow/login" in u:
            return False
        if u.rstrip("/").endswith("x.com/home"):
            return True
    # ambiguous; treat as logged-in only if not on a login URL
    return "/login" not in _current_url(send)


def main():
    handle = resolve_handle()
    if not handle:
        print("restore_twitter_session: no handle configured; skipping", file=sys.stderr)
        return 0

    try:
        ws, send = _attach()
    except Exception as e:
        print(f"restore_twitter_session: cannot attach to {CDP}: {e}", file=sys.stderr)
        return 1

    try:
        if _logged_in(send):
            print(f"restore_twitter_session: already logged in as @{handle}; no-op")
            return 0

        print(f"restore_twitter_session: logged out, fetching stored cookies for @{handle}...")
        resp = api_get("/api/v1/twitter/session-cookies", query={"handle": handle})
        data = (resp or {}).get("data") or {}
        cookies = data.get("cookies") or []
        if not cookies:
            print("restore_twitter_session: no stored cookies; manual re-login required", file=sys.stderr)
            return 1

        # CDP Network.setCookies wants url or domain/path. The stored cookies are
        # already CDP-shaped (from Network.getAllCookies), so pass them straight.
        send("Network.enable")
        ok_count = 0
        for c in cookies:
            params = {k: c[k] for k in (
                "name", "value", "domain", "path", "secure", "httpOnly",
                "sameSite", "expires") if k in c and c[k] is not None}
            r = send("Network.setCookie", params)
            if r.get("result", {}).get("success", True):
                ok_count += 1
        print(f"restore_twitter_session: injected {ok_count}/{len(cookies)} cookies")

        if _logged_in(send):
            print(f"restore_twitter_session: RESTORED @{handle} session")
            return 0
        print("restore_twitter_session: injection done but still logged out (cookies may be expired)", file=sys.stderr)
        return 1
    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
