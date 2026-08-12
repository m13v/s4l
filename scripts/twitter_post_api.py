#!/usr/bin/env python3
"""twitter_post_api.py — API posting transport (twitterapi.io write lane).

The hosted-lane counterpart of `twitter_browser.py reply`: posts X replies and
tweets through twitterapi.io's v2 write endpoints instead of driving the CDP
Chrome. Decision 2026-08-12 (user): twitterapi.io, not the official X API.

How the write lane works (docs.twitterapi.io):
  1. POST /twitter/user_login_v2 {user_name, email, password, totp_secret?,
     proxy} -> login_cookie. twitterapi.io performs the X login server-side;
     ~$0.005/call. Cookies "remain valid indefinitely" with good standing +
     residential proxy, so login is a RARE bootstrap step, not per-post.
  2. POST /twitter/create_tweet_v2 {auth_session (login_cookies), tweet_text,
     reply_to_tweet_id?, proxy} -> tweet_id.
  All write calls REQUIRE a residential proxy URL (http://user:pass@host:port).

Credential resolution (env wins, then macOS keychain; hosted containers use
env, the operator Mac uses keychain):
  API key:      $TWITTERAPI_IO_KEY, else keychain `twitterapi-io-key`, else
                keychain `twitterapi-io-m13v` (legacy entry).
  login cookie: $S4L_TAPI_LOGIN_COOKIE, else keychain
                `twitterapi-io-login-cookie` (account = X handle).
  proxy:        $S4L_TAPI_PROXY, else keychain `decodo-residential-proxy`.

CLI (all print a single JSON result line to stdout; secrets never printed):
  login  --handle m13v_ --email i@m13v.com [--password-keychain x.com]
         performs user_login_v2 and stores the cookie in the keychain.
  reply  <tweet_id_or_url> <text...>
  tweet  <text...>
  delete <tweet_id>

Exit codes: 0 ok; 1 error; 3 auth (login cookie missing/expired -> re-login).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.twitterapi.io"
_STATUS_ID_RE = re.compile(r"/status/(\d+)")


def _keychain(service: str) -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def _keychain_store(service: str, account: str, value: str) -> bool:
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-a", account, "-s", service,
             "-w", value, "-U"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _api_key() -> str | None:
    return (
        os.environ.get("TWITTERAPI_IO_KEY", "").strip()
        or _keychain("twitterapi-io-key")
        or _keychain("twitterapi-io-m13v")
    )


def _login_cookie() -> str | None:
    return (
        os.environ.get("S4L_TAPI_LOGIN_COOKIE", "").strip()
        or _keychain("twitterapi-io-login-cookie")
    )


def _proxy() -> str | None:
    return (
        os.environ.get("S4L_TAPI_PROXY", "").strip()
        or _keychain("decodo-residential-proxy")
    )


def _post(path: str, body: dict, key: str, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"status": "error", "msg": f"http_{e.code}"}


def _tweet_id(arg: str) -> str | None:
    if re.fullmatch(r"\d{5,25}", arg):
        return arg
    m = _STATUS_ID_RE.search(arg)
    return m.group(1) if m else None


def _emit(obj: dict) -> None:
    print(json.dumps(obj))


def cmd_login(ns) -> int:
    key = _api_key()
    if not key:
        _emit({"status": "error", "msg": "no twitterapi.io API key"})
        return 1
    proxy = _proxy()
    if not proxy:
        _emit({"status": "error", "msg": "no residential proxy configured"})
        return 1
    password = os.environ.get("S4L_TAPI_X_PASSWORD", "").strip() or _keychain(
        ns.password_keychain
    )
    if not password:
        _emit({"status": "error", "msg": f"no password in env or keychain {ns.password_keychain!r}"})
        return 1
    body = {
        "user_name": ns.handle,
        "email": ns.email,
        "password": password,
        "proxy": proxy,
    }
    totp = os.environ.get("S4L_TAPI_TOTP_SECRET", "").strip() or (
        _keychain(ns.totp_keychain) if ns.totp_keychain else None
    )
    if totp:
        body["totp_secret"] = totp
    out = _post("/twitter/user_login_v2", body, key, timeout=120.0)
    cookie = out.get("login_cookie") or out.get("login_cookies") or ""
    if out.get("status") == "success" and cookie:
        stored = _keychain_store("twitterapi-io-login-cookie", ns.handle, cookie)
        _emit({"status": "success", "cookie_stored_in_keychain": stored,
               "cookie_len": len(cookie)})
        return 0
    _emit({"status": "error", "msg": out.get("msg") or out.get("message") or str(out)[:300]})
    return 1


def _write_call(path: str, extra: dict) -> int:
    key = _api_key()
    if not key:
        _emit({"status": "error", "msg": "no twitterapi.io API key"})
        return 1
    cookie = _login_cookie()
    if not cookie:
        _emit({"status": "auth", "msg": "no login cookie; run `twitter_post_api.py login` first"})
        return 3
    proxy = _proxy()
    if not proxy:
        _emit({"status": "error", "msg": "no residential proxy configured"})
        return 1
    body = {"login_cookies": cookie, "proxy": proxy}
    body.update(extra)
    out = _post(path, body, key, timeout=90.0)
    status = out.get("status")
    msg = (out.get("msg") or out.get("message") or "")
    if status == "success":
        _emit({"status": "success", "tweet_id": out.get("tweet_id") or out.get("data", {}).get("tweet_id"), "msg": msg})
        return 0
    if re.search(r"cookie|login|session|auth", msg, re.IGNORECASE):
        _emit({"status": "auth", "msg": msg[:300]})
        return 3
    _emit({"status": "error", "msg": msg[:300] or str(out)[:300]})
    return 1


def cmd_reply(ns) -> int:
    tid = _tweet_id(ns.target)
    if not tid:
        _emit({"status": "error", "msg": f"cannot parse tweet id from {ns.target!r}"})
        return 1
    return _write_call(
        "/twitter/create_tweet_v2",
        {"tweet_text": " ".join(ns.text), "reply_to_tweet_id": tid},
    )


def cmd_tweet(ns) -> int:
    return _write_call("/twitter/create_tweet_v2", {"tweet_text": " ".join(ns.text)})


def cmd_delete(ns) -> int:
    tid = _tweet_id(ns.target)
    if not tid:
        _emit({"status": "error", "msg": f"cannot parse tweet id from {ns.target!r}"})
        return 1
    return _write_call("/twitter/delete_tweet_v2", {"tweet_id": tid})


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="user_login_v2 -> store cookie in keychain")
    pl.add_argument("--handle", required=True)
    pl.add_argument("--email", required=True)
    pl.add_argument("--password-keychain", default="x.com",
                    help="keychain service holding the X password")
    pl.add_argument("--totp-keychain", default=None)
    pl.set_defaults(fn=cmd_login)

    pr = sub.add_parser("reply", help="reply to a tweet (id or URL)")
    pr.add_argument("target")
    pr.add_argument("text", nargs="+")
    pr.set_defaults(fn=cmd_reply)

    pt = sub.add_parser("tweet", help="post a standalone tweet")
    pt.add_argument("text", nargs="+")
    pt.set_defaults(fn=cmd_tweet)

    pd = sub.add_parser("delete", help="delete a tweet by id")
    pd.add_argument("target")
    pd.set_defaults(fn=cmd_delete)

    ns = p.parse_args()
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main())
