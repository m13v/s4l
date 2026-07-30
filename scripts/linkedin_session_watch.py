#!/usr/bin/env python3
"""LinkedIn session canary: detect a dropped session in ~60s instead of ~30min.

What problem this solves
------------------------
`detect-gate` only probes the session at the START of a pipeline run. On
2026-07-20 it reported "session healthy (feed renders)" at 20:27:31Z, the
session was killed somewhere around 20:50Z mid-comment, and nothing noticed
until the next run-linkedin fire at 20:57:12Z. That is a ~30 minute blind
window in which the pipeline believed it was logged in.

This canary closes that window by reading the `li_at` session cookie straight
out of the ALREADY-RUNNING harness Chrome over CDP. Two properties matter:

  1. It costs ZERO LinkedIn traffic. CDP is a local debugging channel, so this
     adds no request footprint to an account that is already flagged. That is
     why we do not simply fetch /feed/ on a timer.
  2. It reads the LIVE browser, not the on-disk Cookies sqlite file. The
     on-disk store lags badly: on 2026-07-20 the `linkedin` profile's Cookies
     db had an mtime of 2026-07-02 while the live context still held a valid
     li_at. Reading the file would produce false alarms.

Conservative by design: if Chrome is down, or CDP does not answer, or the
cookie read fails, we report `unknown` and do NOTHING. A canary that cries wolf
gets ignored, and engaging the killswitch is disruptive (it halts nine launchd
jobs). We only act on a positive, well-formed "Chrome is up, cookies read fine,
li_at is absent".

CLI
---
    linkedin_session_watch.py check            # report only, never engages
    linkedin_session_watch.py check --engage   # engage killswitch if li_at gone
    linkedin_session_watch.py check --json

Exit codes:
    0  li_at present (healthy)
    1  li_at absent (session dropped)
    2  unknown / not checkable (Chrome down, CDP silent) - NOT an error

Intended use: launchd every 60s with --engage.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

CDP_URL = os.environ.get("LINKEDIN_CDP_URL", "http://127.0.0.1:9556")
REPO_DIR = os.path.expanduser("~/social-autoposter")
KILLSWITCH = os.path.join(REPO_DIR, "scripts", "linkedin_killswitch.py")
STATE_PATH = os.path.expanduser(
    "~/.claude/social-autoposter/linkedin.session_canary.json"
)
TIMEOUT_S = 8


def _browser_ws_url():
    """Browser-level CDP websocket URL, or None if Chrome is not reachable."""
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8")).get("webSocketDebuggerUrl")
    except Exception:
        return None


def _all_cookies(ws_url):
    """Storage.getCookies at browser scope. Returns list, or None on failure.

    Uses websocket-client, which is present on the pipeline's /opt/homebrew/bin/python3.
    We deliberately avoid playwright's connect_over_cdp here: this runs every
    minute alongside live pipeline work, and a passive single-command socket is
    far less likely to perturb a run than attaching a full automation client.
    """
    try:
        import websocket  # websocket-client
    except ImportError:
        return None
    ws = None
    try:
        # suppress_origin is REQUIRED: websocket-client otherwise sends an
        # Origin header and Chrome rejects the handshake with
        # "403 Rejected an incoming WebSocket connection from the ... origin".
        ws = websocket.create_connection(
            ws_url, timeout=TIMEOUT_S, suppress_origin=True
        )
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
        # The browser endpoint can interleave events; read until our reply lands.
        for _ in range(20):
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    return None
                return msg.get("result", {}).get("cookies", [])
        return None
    except Exception:
        return None
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def check():
    now = datetime.now(timezone.utc).isoformat()
    ws_url = _browser_ws_url()
    if not ws_url:
        return {"status": "unknown", "reason": "harness Chrome not reachable on CDP",
                "checked_at": now}

    cookies = _all_cookies(ws_url)
    if cookies is None:
        return {"status": "unknown", "reason": "CDP cookie read failed",
                "checked_at": now}

    li = [c for c in cookies if "linkedin.com" in (c.get("domain") or "")]
    has_li_at = any(c.get("name") == "li_at" for c in li)
    has_li_rm = any(c.get("name") == "li_rm" for c in li)

    return {
        "status": "healthy" if has_li_at else "dropped",
        "reason": ("li_at present" if has_li_at
                   else "li_at absent from live harness Chrome"),
        "linkedin_cookie_count": len(li),
        "has_li_rm": has_li_rm,
        "checked_at": now,
    }


def _killswitch_active():
    try:
        return subprocess.run(
            ["/opt/homebrew/bin/python3", KILLSWITCH, "check"],
            capture_output=True, timeout=30,
        ).returncode != 0
    except Exception:
        return True  # assume active; never double-engage on uncertainty


def engage(detail):
    """Engage the killswitch. Idempotent upstream: the FIRST signal wins."""
    if _killswitch_active():
        return {"engaged": False, "note": "killswitch already active"}
    try:
        p = subprocess.run(
            ["/opt/homebrew/bin/python3", KILLSWITCH, "engage",
             "--signal", "li_at_cleared", "--detail", detail],
            capture_output=True, text=True, timeout=120,
        )
        return {"engaged": p.returncode == 0,
                "stdout": (p.stdout or "").strip()[:300],
                "stderr": (p.stderr or "").strip()[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"engaged": False, "error": str(exc)[:200]}


def _write_state(result):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["check"])
    ap.add_argument("--engage", action="store_true",
                    help="engage the killswitch when li_at is absent")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = check()

    if result["status"] == "dropped" and args.engage:
        result["killswitch"] = engage(
            "session canary: li_at absent from live harness Chrome"
        )

    _write_state(result)

    if args.json:
        print(json.dumps(result))
    else:
        print(f"{result['status'].upper()}: {result['reason']}")

    return {"healthy": 0, "dropped": 1, "unknown": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
