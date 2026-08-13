#!/usr/bin/env python3
"""twitter_post_api.py — API posting transport (GetXAPI write lane).

The hosted-lane counterpart of `twitter_browser.py reply`: posts X replies,
tweets, and deletions through GetXAPI (api.getxapi.com) instead of driving the
CDP Chrome. Bring-your-own-cookie model: the tenant's live X session cookies
(auth_token, plus ct0/twid when available) ride each request; GetXAPI executes
the action against X's private endpoints and returns the tweet id.

Provider history (2026-08-12): twitterapi.io's write lane was implemented
first but requires a server-side password login (user_login_v2) that cannot
pass SMS-2FA / login challenges, and its login_cookies blob rejects raw
session cookies. GetXAPI accepts the session cookies we already hold (the
same ones the browser transport uses), needs no password, no TOTP, and its
proxy is optional. Live-proven same day: reply 2087730228160278671 posted
200-clean under our own thread. One implementation only, per the
minimize-code-footprint rule; if the provider ever changes again, REPLACE
this lane, do not layer a second one.

Credential resolution (env wins, then macOS keychain / mirror file; hosted
containers use env or per-tenant DB values, the operator Mac uses local
sources):
  API key:  $GETXAPI_KEY, else keychain `getxapi-key`.
  cookies:  $S4L_X_AUTH_TOKEN (+ optional $S4L_X_CT0, $S4L_X_TWID), else the
            harness cookie mirror JSON at $S4L_X_COOKIE_MIRROR (default
            ~/.claude/browser-profiles/browser-harness.x-cookies.json — the
            durable 0600 mirror connect_x refreshes on every connect).
  proxy:    $S4L_TAPI_PROXY, else keychain `decodo-residential-proxy`, else
            none (GetXAPI's proxy field is optional; we pass ours for IP
            hygiene so account actions egress residential).

CLI (single JSON result line on stdout; secrets never printed):
  reply  <tweet_id_or_url> <text...>
  tweet  <text...>
  delete <tweet_id_or_url>

Exit codes: 0 ok; 1 error; 3 auth (cookies missing/stale -> reconnect X).
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

API = "https://api.getxapi.com"
DEFAULT_MIRROR = os.path.expanduser(
    "~/.claude/browser-profiles/browser-harness.x-cookies.json"
)
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


def _api_key() -> str | None:
    return os.environ.get("GETXAPI_KEY", "").strip() or _keychain("getxapi-key")


def _proxy() -> str | None:
    return (
        os.environ.get("S4L_TAPI_PROXY", "").strip()
        or _keychain("decodo-residential-proxy")
    )


def _cookies() -> dict:
    """{auth_token, ct0?, twid?} from env, else the harness cookie mirror."""
    tok = os.environ.get("S4L_X_AUTH_TOKEN", "").strip()
    if tok:
        out = {"auth_token": tok}
        for env, field in (("S4L_X_CT0", "ct0"), ("S4L_X_TWID", "twid")):
            v = os.environ.get(env, "").strip()
            if v:
                out[field] = v
        return out
    path = os.environ.get("S4L_X_COOKIE_MIRROR", "").strip() or DEFAULT_MIRROR
    try:
        with open(path) as f:
            mirror = json.load(f)
    except Exception:
        return {}
    vals = {
        c.get("name"): c.get("value")
        for c in mirror.get("cookies", [])
        if c.get("name") in ("auth_token", "ct0", "twid")
    }
    return {k: v for k, v in vals.items() if v}


def _tweet_id(arg: str) -> str | None:
    if re.fullmatch(r"\d{5,25}", arg):
        return arg
    m = _STATUS_ID_RE.search(arg)
    return m.group(1) if m else None


def _emit(obj: dict) -> None:
    print(json.dumps(obj))


def _call(path: str, extra: dict) -> int:
    key = _api_key()
    if not key:
        _emit({"status": "error", "msg": "no GetXAPI key ($GETXAPI_KEY / keychain getxapi-key)"})
        return 1
    ck = _cookies()
    if not ck.get("auth_token"):
        _emit({"status": "auth", "msg": "no X session cookies (env or cookie mirror); reconnect X"})
        return 3
    body = dict(ck)
    proxy = _proxy()
    if proxy:
        body["proxy"] = proxy
    body.update(extra)
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            out = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        tail = ""
        try:
            tail = e.read().decode()[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            _emit({"status": "auth", "msg": f"http_{e.code}: {tail}"})
            return 3
        _emit({"status": "error", "msg": f"http_{e.code}: {tail}"})
        return 1
    except Exception as e:
        _emit({"status": "error", "msg": f"{type(e).__name__}: {e}"})
        return 1

    # Response shapes: {success/status, tweet_id | data:{...}} — be lenient.
    tid = (
        out.get("tweet_id")
        or (out.get("data") or {}).get("tweet_id")
        or (out.get("data") or {}).get("id")
    )
    ok = out.get("success") is True or out.get("status") in ("success", "ok") or bool(tid)
    msg = str(out.get("msg") or out.get("message") or out.get("error") or "")
    if ok:
        _emit({"status": "success", "tweet_id": tid, "msg": msg[:200]})
        return 0
    if re.search(r"auth|cookie|session|401|403", msg, re.IGNORECASE):
        _emit({"status": "auth", "msg": msg[:300]})
        return 3
    _emit({"status": "error", "msg": (msg or str(out))[:300]})
    return 1


def cmd_reply(ns) -> int:
    tid = _tweet_id(ns.target)
    if not tid:
        _emit({"status": "error", "msg": f"cannot parse tweet id from {ns.target!r}"})
        return 1
    return _call("/twitter/tweet/create", {"text": " ".join(ns.text), "reply_to_tweet_id": tid})


def cmd_tweet(ns) -> int:
    return _call("/twitter/tweet/create", {"text": " ".join(ns.text)})


def cmd_delete(ns) -> int:
    tid = _tweet_id(ns.target)
    if not tid:
        _emit({"status": "error", "msg": f"cannot parse tweet id from {ns.target!r}"})
        return 1
    return _call("/twitter/tweet/delete", {"tweet_id": tid})


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("reply", help="reply to a tweet (id or URL)")
    pr.add_argument("target")
    pr.add_argument("text", nargs="+")
    pr.set_defaults(fn=cmd_reply)

    pt = sub.add_parser("tweet", help="post a standalone tweet")
    pt.add_argument("text", nargs="+")
    pt.set_defaults(fn=cmd_tweet)

    pd = sub.add_parser("delete", help="delete a tweet by id or URL (own tweets only)")
    pd.add_argument("target")
    pd.set_defaults(fn=cmd_delete)

    ns = p.parse_args()
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main())
