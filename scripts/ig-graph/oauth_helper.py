#!/usr/bin/env python3
"""
Instagram Graph API OAuth helper.

Flow (Instagram API with Instagram Login, the 2024+ "no FB Page" path):
  1. Open `https://www.instagram.com/oauth/authorize?...` in the user's logged-in
     IG browser session.
  2. Spin up a localhost HTTPS server on REDIRECT_URI to capture ?code=...
  3. Exchange the code for a short-lived user access token at
     https://api.instagram.com/oauth/access_token
  4. Exchange short-lived (1h) for a long-lived 60-day token at
     https://graph.instagram.com/access_token
  5. Probe /me?fields=id,username,user_id,account_type to confirm + grab
     the Instagram User ID (`user_id`) needed for /<ig-user-id>/media calls.

App Identifiers:
  - IG App ID:     799089372897360       (the "Instagram" sub-app inside Meta App)
  - FB App ID:     2791269617874851      (the parent Meta App, used for FB-login flow)
  - App Secret:    in keychain "Instagram API social-autoposter" (32-char hex)

Usage:
  ./oauth_helper.py authorize         # prints the OAuth URL + waits for callback
  ./oauth_helper.py refresh-long      # refreshes the stored long-lived token
  ./oauth_helper.py whoami            # prints stored token's IG account info

Token storage:
  ~/.config/fazm/ig_graph_token.json   # {access_token, ig_user_id, username, expires_at}
"""

from __future__ import annotations  # PEP 604 unions (dict | None) for Python 3.9 launchd

import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
IG_APP_ID = "799089372897360"
APP_SECRET_KEYCHAIN = "Instagram API social-autoposter"
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8443
REDIRECT_PATH = "/oauth/callback"
REDIRECT_URI = f"https://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"
# All Business-API scopes (testers in dev mode are auto-granted these)
SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
    "instagram_business_manage_insights",
]
TOKEN_PATH = Path.home() / ".config" / "fazm" / "ig_graph_token.json"

# -----------------------------------------------------------------------------
# Keychain
# -----------------------------------------------------------------------------
def get_app_secret() -> str:
    out = subprocess.check_output(
        ["security", "find-generic-password", "-s", APP_SECRET_KEYCHAIN, "-w"],
        text=True,
    ).strip()
    if len(out) != 32:
        raise RuntimeError(f"unexpected app secret length {len(out)} (expected 32)")
    return out


# -----------------------------------------------------------------------------
# OAuth helpers
# -----------------------------------------------------------------------------
def build_authorize_url() -> str:
    params = {
        "client_id": IG_APP_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "force_authentication": "1",
        "enable_fb_login": "0",
    }
    return "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode(params)


def exchange_code_for_short_token(code: str) -> dict:
    """Short-lived token (1h)."""
    data = urllib.parse.urlencode({
        "client_id": IG_APP_ID,
        "client_secret": get_app_secret(),
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }).encode()
    req = urllib.request.Request(
        "https://api.instagram.com/oauth/access_token",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def exchange_short_for_long_token(short_token: str) -> dict:
    """Long-lived token (60d), refreshable."""
    qs = urllib.parse.urlencode({
        "grant_type": "ig_exchange_token",
        "client_secret": get_app_secret(),
        "access_token": short_token,
    })
    url = f"https://graph.instagram.com/access_token?{qs}"
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def refresh_long_token(long_token: str) -> dict:
    qs = urllib.parse.urlencode({
        "grant_type": "ig_refresh_token",
        "access_token": long_token,
    })
    url = f"https://graph.instagram.com/refresh_access_token?{qs}"
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def me_lookup(token: str) -> dict:
    qs = urllib.parse.urlencode({
        "fields": "id,user_id,username,account_type",
        "access_token": token,
    })
    url = f"https://graph.instagram.com/v23.0/me?{qs}"
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


# -----------------------------------------------------------------------------
# Localhost HTTPS callback server (uses self-signed cert, browser will warn once)
# -----------------------------------------------------------------------------
def ensure_self_signed_cert() -> tuple[str, str]:
    cert_dir = Path.home() / ".config" / "fazm"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "oauth_localhost.crt"
    key_path = cert_dir / "oauth_localhost.key"
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key_path), "-out", str(cert_path),
            "-days", "3650", "-nodes",
            "-subj", "/CN=localhost",
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return str(cert_path), str(key_path)


class _CodeCapture(http.server.BaseHTTPRequestHandler):
    captured: dict | None = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_response(404); self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(parsed.query))
        type(self).captured = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h2>OAuth code captured.</h2>"
            b"<p>You can close this tab and return to your terminal.</p>"
        )

    def log_message(self, *args, **kwargs):  # silence
        pass


def wait_for_code(timeout: int = 300) -> dict:
    import ssl as _ssl
    cert, key = ensure_self_signed_cert()
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd = socketserver.TCPServer((REDIRECT_HOST, REDIRECT_PORT), _CodeCapture)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.timeout = 1
    deadline = time.time() + timeout
    while time.time() < deadline:
        httpd.handle_request()
        if _CodeCapture.captured:
            httpd.server_close()
            return _CodeCapture.captured
    httpd.server_close()
    raise TimeoutError("OAuth callback did not arrive within timeout")


# -----------------------------------------------------------------------------
# Token persistence
# -----------------------------------------------------------------------------
def save_token(token_obj: dict):
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token_obj, indent=2))
    os.chmod(TOKEN_PATH, 0o600)


def load_token() -> dict:
    return json.loads(TOKEN_PATH.read_text())


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def cmd_authorize():
    url = build_authorize_url()
    print("\nOAuth authorize URL (open in the browser logged in as @matt_diak):\n")
    print(url, "\n")
    # Try to open it in the user's default browser. macOS: `open` will use Arc on
    # this machine, but Arc forwards into the same Chrome session matt_diak uses.
    try:
        subprocess.run(["open", url], check=False)
    except Exception:
        pass
    print(f"Listening on {REDIRECT_URI} (HTTPS, self-signed cert) ...")
    print("Browser will warn about the cert; click 'Advanced' -> 'Proceed'.\n")
    captured = wait_for_code(timeout=600)
    if "error" in captured:
        print("OAuth ERROR:", json.dumps(captured, indent=2))
        sys.exit(1)
    code = captured.get("code")
    if not code:
        print("No code in callback. Captured:", captured); sys.exit(1)
    print("Got code, exchanging for short-lived token ...")
    short = exchange_code_for_short_token(code)
    if "access_token" not in short:
        print("Short-token error:", short); sys.exit(1)
    print("Exchanging short for long-lived (60d) token ...")
    long_ = exchange_short_for_long_token(short["access_token"])
    if "access_token" not in long_:
        print("Long-token error:", long_); sys.exit(1)
    me = me_lookup(long_["access_token"])
    obj = {
        "access_token": long_["access_token"],
        "expires_in": long_.get("expires_in"),
        "expires_at": int(time.time() + long_.get("expires_in", 5184000)),
        "token_type": long_.get("token_type", "bearer"),
        "ig_id": me.get("id"),
        "ig_user_id": me.get("user_id") or me.get("id"),
        "username": me.get("username"),
        "account_type": me.get("account_type"),
        "scopes_requested": SCOPES,
    }
    save_token(obj)
    print("\nSUCCESS. Saved token to:", TOKEN_PATH)
    print(json.dumps({**obj, "access_token": obj["access_token"][:10] + "..."}, indent=2))


def cmd_refresh_long():
    cur = load_token()
    new = refresh_long_token(cur["access_token"])
    if "access_token" not in new:
        print("Refresh error:", new); sys.exit(1)
    cur["access_token"] = new["access_token"]
    cur["expires_in"] = new.get("expires_in")
    cur["expires_at"] = int(time.time() + new.get("expires_in", 5184000))
    save_token(cur)
    print("Refreshed. New expiry:", cur["expires_at"])


def cmd_whoami():
    cur = load_token()
    print(json.dumps(me_lookup(cur["access_token"]), indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "authorize"
    {"authorize": cmd_authorize,
     "refresh-long": cmd_refresh_long,
     "whoami": cmd_whoami}.get(cmd, cmd_authorize)()
