#!/usr/bin/env python3
"""TikTok OAuth helper for Meditation Fellow Studio (sandbox).

Subcommands:
  authorize           Print + open the authorize URL for the sandbox app.
                      Sign in as @meditation.fellow (the configured target user)
                      and grant access. TikTok redirects to
                      https://app.s4l.ai/oauth/tiktok/callback?code=...
                      The dashboard callback page just displays the code; copy
                      it and pass it to `exchange`.
  exchange <code>     Trade the authorization code for an access + refresh
                      token via the TikTok v2 token endpoint, then write the
                      result to ~/tiktok-content-api/.env. Mirrors the IG
                      Graph helper layout so post_to_tiktok.py can mirror
                      post_to_ig.py.
  refresh             Use the stored refresh_token to mint a fresh access
                      token. Run this from cron before each posting cycle.
  info                Print stored token metadata (no secrets revealed).

Sandbox vs production credentials are pulled from the macOS keychain:
  TikTok Sandbox Client Key
  TikTok Sandbox Client Secret
Production switch will use TikTok Client Key / TikTok Client Secret instead.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REDIRECT_URI = "https://app.s4l.ai/oauth/tiktok/callback"
SCOPES = "user.info.basic,video.upload,video.publish"
AUTH_URL_BASE = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

ENV_PATH = Path.home() / "tiktok-content-api" / ".env"
SANDBOX = os.environ.get("TIKTOK_MODE", "sandbox").lower() == "sandbox"


def keychain(service: str) -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def creds() -> tuple[str, str]:
    if SANDBOX:
        return (
            keychain("TikTok Sandbox Client Key"),
            keychain("TikTok Sandbox Client Secret"),
        )
    return (
        keychain("TikTok Client Key"),
        keychain("TikTok Client Secret"),
    )


def authorize() -> None:
    client_key, _ = creds()
    state = secrets.token_urlsafe(16)
    params = {
        "client_key": client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    url = AUTH_URL_BASE + "?" + urlencode(params)
    print("Authorize URL (sandbox=" + str(SANDBOX) + "):")
    print(url)
    print()
    print(f"State (will appear on the callback page): {state}")
    print()
    # Open in user's default browser. The user must be logged in to TikTok as
    # @meditation.fellow (or another configured target user) in that browser.
    try:
        subprocess.run(["open", url], check=False)
    except FileNotFoundError:
        pass
    print("Opened in browser. After authorizing, copy the `code` from the")
    print("callback page and run:")
    print(f"  python3 {sys.argv[0]} exchange <code>")


def _post_token(form: dict) -> dict:
    data = urlencode(form).encode()
    req = Request(
        TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urlopen(req, timeout=30) as r:
            body = r.read()
    except HTTPError as e:
        body = e.read()
        print(f"HTTP {e.code} from token endpoint:")
        print(body.decode(errors="replace"))
        sys.exit(1)
    try:
        return json.loads(body)
    except ValueError:
        print("Non-JSON response from token endpoint:")
        print(body.decode(errors="replace"))
        sys.exit(1)


def exchange(code: str) -> None:
    client_key, client_secret = creds()
    resp = _post_token({
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    })
    if resp.get("error") and resp["error"] not in (None, "", "ok"):
        print("Token-exchange error:")
        print(json.dumps(resp, indent=2))
        sys.exit(1)
    now = int(time.time())
    env = _read_env()
    env.update({
        "TIKTOK_MODE": "sandbox" if SANDBOX else "production",
        "TIKTOK_OPEN_ID": resp.get("open_id", ""),
        "TIKTOK_ACCESS_TOKEN": resp.get("access_token", ""),
        "TIKTOK_REFRESH_TOKEN": resp.get("refresh_token", ""),
        "TIKTOK_ACCESS_EXPIRES_AT": str(now + int(resp.get("expires_in", 86400))),
        "TIKTOK_REFRESH_EXPIRES_AT": str(now + int(resp.get("refresh_expires_in", 31536000))),
        "TIKTOK_SCOPE": resp.get("scope", SCOPES),
        "TIKTOK_TOKEN_TYPE": resp.get("token_type", "Bearer"),
    })
    _write_env(env)
    print(f"Tokens written to {ENV_PATH}")
    print(f"  open_id   : {env['TIKTOK_OPEN_ID']}")
    print(f"  scope     : {env['TIKTOK_SCOPE']}")
    print(f"  access ttl: {resp.get('expires_in')}s")
    print(f"  refresh ttl: {resp.get('refresh_expires_in')}s")


def refresh() -> None:
    env = _read_env()
    rt = env.get("TIKTOK_REFRESH_TOKEN")
    if not rt:
        print("No refresh_token stored. Run `authorize` + `exchange` first.")
        sys.exit(1)
    client_key, client_secret = creds()
    resp = _post_token({
        "client_key": client_key,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": rt,
    })
    if resp.get("error") and resp["error"] not in (None, "", "ok"):
        print("Refresh error:")
        print(json.dumps(resp, indent=2))
        sys.exit(1)
    now = int(time.time())
    env["TIKTOK_ACCESS_TOKEN"] = resp.get("access_token", env.get("TIKTOK_ACCESS_TOKEN", ""))
    env["TIKTOK_REFRESH_TOKEN"] = resp.get("refresh_token", env.get("TIKTOK_REFRESH_TOKEN", ""))
    env["TIKTOK_ACCESS_EXPIRES_AT"] = str(now + int(resp.get("expires_in", 86400)))
    env["TIKTOK_REFRESH_EXPIRES_AT"] = str(now + int(resp.get("refresh_expires_in", 31536000)))
    _write_env(env)
    print("Tokens refreshed.")


def info() -> None:
    env = _read_env()
    if not env:
        print("(no tokens stored)")
        return
    now = int(time.time())
    for k in ("TIKTOK_MODE", "TIKTOK_OPEN_ID", "TIKTOK_SCOPE", "TIKTOK_TOKEN_TYPE"):
        if env.get(k):
            print(f"{k}: {env[k]}")
    for k in ("TIKTOK_ACCESS_EXPIRES_AT", "TIKTOK_REFRESH_EXPIRES_AT"):
        v = env.get(k)
        if not v:
            continue
        try:
            ttl = int(v) - now
        except ValueError:
            ttl = None
        print(f"{k}: {v}{' (' + str(ttl) + 's left)' if ttl is not None else ''}")
    for k in ("TIKTOK_ACCESS_TOKEN", "TIKTOK_REFRESH_TOKEN"):
        v = env.get(k, "")
        masked = (v[:6] + "..." + v[-4:]) if len(v) > 12 else "(empty)"
        print(f"{k}: {masked}")


def _read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    env: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _write_env(env: dict[str, str]) -> None:
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{k}={v}" for k, v in env.items()) + "\n"
    ENV_PATH.write_text(body)
    os.chmod(ENV_PATH, 0o600)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "authorize":
        authorize()
    elif cmd == "exchange":
        if len(argv) < 3:
            print("Usage: oauth_helper.py exchange <code>")
            return 1
        exchange(argv[2])
    elif cmd == "refresh":
        refresh()
    elif cmd == "info":
        info()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
