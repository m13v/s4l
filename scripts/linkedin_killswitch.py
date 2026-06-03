#!/usr/bin/env python3
"""LinkedIn pipeline killswitch.

Single source of truth for "LinkedIn is throttling / has revoked our
session; do not run anything that talks to LinkedIn until a human
re-auths and clears the flag".

State lives at ~/.claude/social-autoposter/linkedin.killswitch as JSON:

    {
      "signal": "http_999" | "authwall_redirect" | "throttle_no_pagination"
                | "li_at_cleared" | "session_invalid_marker" | "manual",
      "detail": "...",
      "ts": "2026-05-27T21:20:58Z",
      "run_log_path": "/Users/matthewdi/social-autoposter/skill/logs/...log",
      "pid": 12345,
      "user_to_resolve": "Re-auth LinkedIn in harness Chrome, then clear."
    }

Every LinkedIn entrypoint (skill/*linkedin*.sh, engage-dm-replies.sh)
calls `check` at the top and exits 0 if active. `engage()` is
idempotent: the FIRST signal wins, later signals append to a
trail file so we can see what cascaded.

CLI:
    python3 scripts/linkedin_killswitch.py check        # exit 0 if clear, 1 if active
    python3 scripts/linkedin_killswitch.py status       # print payload (json), exit 0
    python3 scripts/linkedin_killswitch.py engage \\
        --signal http_999 \\
        --detail "GET /in/me/recent-activity/comments/ -> 999" \\
        --run-log /path/to/log
    python3 scripts/linkedin_killswitch.py clear        # remove the flag (human ack)

The shell pattern (after `set -euo pipefail`, before any work):

    if ! /opt/homebrew/bin/python3 "$REPO_DIR/scripts/linkedin_killswitch.py" check >/dev/null 2>&1; then
        log "LINKEDIN_KILLSWITCH active. To resume: re-auth LinkedIn in harness Chrome, then:"
        log "    python3 $REPO_DIR/scripts/linkedin_killswitch.py clear"
        exit 0
    fi

Email alert on engage: re-uses the same Gmail token strike_alert.py
uses. ONE email per engage call (idempotency in the file prevents
re-emailing on every cron tick). Subject prefix "[LI KILL]".
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText


# State paths are env-overridable so the auto-recovery job can be tested
# against a throwaway killswitch file without touching the live one.
STATE_DIR = os.path.expanduser(
    os.environ.get("LINKEDIN_KILLSWITCH_DIR", "~/.claude/social-autoposter")
)
STATE_FILE = os.path.expanduser(
    os.environ.get("LINKEDIN_KILLSWITCH_FILE", os.path.join(STATE_DIR, "linkedin.killswitch"))
)
TRAIL_FILE = os.path.expanduser(
    os.environ.get(
        "LINKEDIN_KILLSWITCH_TRAIL", os.path.join(STATE_DIR, "linkedin.killswitch.trail.jsonl")
    )
)

GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]
NOTIFICATION_EMAIL = os.environ.get("NOTIFICATION_EMAIL", "i@m13v.com")

# Auto-recovery (2026-06-03): after the killswitch has been active this long,
# an hourly launchd job (skill/linkedin-recovery.sh) runs a gentle read-only
# probe of LinkedIn. If the session is healthy again, it clears the flag, which
# resumes every LinkedIn pipeline on its next fire (they all gate on this file).
# The wait protects the account: per the anti-bot rule we let the session sit
# idle ~24h after a 999/authwall before re-touching it, rather than hammering
# the login wall on every cron tick. Override for testing.
RECOVERY_MIN_AGE_HOURS = float(os.environ.get("LINKEDIN_RECOVERY_MIN_AGE_HOURS", "24"))
LINKEDIN_CDP_URL = os.environ.get("LINKEDIN_CDP_URL", "http://127.0.0.1:9556")

VALID_SIGNALS = {
    "http_999",
    "authwall_redirect",
    "checkpoint_redirect",
    "login_redirect",
    "throttle_no_pagination",
    "li_at_cleared",
    "session_invalid_marker",
    "captcha_detected",
    "manual",
}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def is_active():
    return os.path.isfile(STATE_FILE)


def read():
    if not is_active():
        return None
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"signal": "unknown", "detail": "state file unreadable"}


def _parse_ts(ts):
    """Parse an ISO Z timestamp like 2026-06-03T07:23:10Z. None on failure."""
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def age_seconds():
    """Seconds since the killswitch engaged, or None if inactive/unparseable."""
    p = read()
    if not p:
        return None
    dt = _parse_ts(p.get("ts", ""))
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _append_trail(payload):
    _ensure_dir()
    try:
        with open(TRAIL_FILE, "a") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def _scrub_dashes(s):
    if not s:
        return s
    return s.replace("\u2014", ",").replace("\u2013", ",")


def _send_alert_email(payload, first_time):
    """Send an email alert. first_time=True only on the engaging call.

    Best-effort: failure to send must not block engagement (the file
    is the source of truth, not the email)."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not os.path.isfile(GMAIL_TOKEN_PATH):
            return False, "gmail token missing"

        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(GMAIL_TOKEN_PATH, "w") as f:
                f.write(creds.to_json())

        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        tag = "ENGAGED" if first_time else "REPEAT"
        subject = "[LI KILL] {tag} signal={sig}".format(
            tag=tag, sig=payload.get("signal", "?"),
        )

        body_lines = [
            "LinkedIn killswitch " + tag.lower() + ".",
            "",
            "All LinkedIn pipelines on this machine will refuse to run until",
            "a human re-authenticates LinkedIn in the harness Chrome and",
            "clears the flag.",
            "",
            "Signal:   " + str(payload.get("signal", "?")),
            "Detail:   " + str(payload.get("detail", "")),
            "Timestamp: " + str(payload.get("ts", "")),
            "PID:      " + str(payload.get("pid", "")),
            "Run log:  " + str(payload.get("run_log_path", "")),
            "",
            "To resume:",
            "  1. Open harness Chrome (linkedin profile) and sign back in.",
            "  2. Confirm a normal /feed/ page renders without authwall.",
            "  3. Clear the killswitch:",
            "       python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear",
            "",
            "State file: " + STATE_FILE,
            "Trail file: " + TRAIL_FILE,
        ]
        body = _scrub_dashes("\n".join(body_lines))
        subject = _scrub_dashes(subject)

        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = NOTIFICATION_EMAIL
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True, "sent"
    except Exception as exc:
        return False, "send failed: " + str(exc)


def engage(signal, detail="", run_log_path="", extra=None, send_email=True):
    """Engage the killswitch. Idempotent: first signal wins.

    Returns the on-disk payload (either the existing one or the
    newly-written one). Always appends to the trail so we can see
    cascades."""
    if signal not in VALID_SIGNALS:
        signal = "manual"

    payload_new = {
        "signal": signal,
        "detail": str(detail)[:2000],
        "ts": _now_iso(),
        "run_log_path": run_log_path,
        "pid": os.getpid(),
        "user_to_resolve": (
            "Re-auth LinkedIn in harness Chrome, confirm /feed/ renders, "
            "then run: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear"
        ),
    }
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in payload_new:
                payload_new[k] = v

    _ensure_dir()
    _append_trail({"event": "engage_call", **payload_new})

    first_time = not is_active()
    if first_time:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload_new, f, indent=2)
            f.write("\n")
        os.replace(tmp, STATE_FILE)
        on_disk = payload_new
    else:
        on_disk = read() or payload_new

    if send_email:
        ok, msg = _send_alert_email(payload_new, first_time)
        _append_trail({"event": "email_attempt", "ok": ok, "msg": msg, "first_time": first_time})

    return on_disk


def clear():
    """Human ack: remove the flag. Trail row records who cleared it."""
    if not is_active():
        return False
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass
    _append_trail({
        "event": "clear",
        "ts": _now_iso(),
        "pid": os.getpid(),
        "cleared_by": os.environ.get("USER", "?"),
    })
    return True


def _cmd_check(args):
    if is_active():
        sys.exit(1)
    sys.exit(0)


def _cmd_status(args):
    p = read()
    if p is None:
        print(json.dumps({"active": False}))
        sys.exit(0)
    out = {"active": True, **p}
    print(json.dumps(out, indent=2))
    sys.exit(0)


def _cmd_engage(args):
    extra = {}
    if args.extra:
        try:
            extra = json.loads(args.extra)
        except Exception:
            extra = {"raw_extra": args.extra}
    p = engage(
        signal=args.signal,
        detail=args.detail or "",
        run_log_path=args.run_log or "",
        extra=extra,
        send_email=not args.no_email,
    )
    print(json.dumps(p, indent=2))
    sys.exit(0)


def _cmd_clear(args):
    ok = clear()
    print(json.dumps({"cleared": ok}))
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="LinkedIn pipeline killswitch")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="exit 0 if clear, 1 if active (no output)")
    sub.add_parser("status", help="print JSON payload of current state")

    e = sub.add_parser("engage", help="engage the killswitch")
    e.add_argument("--signal", required=True, choices=sorted(VALID_SIGNALS))
    e.add_argument("--detail", default="")
    e.add_argument("--run-log", default="")
    e.add_argument("--extra", default="", help="JSON object of extra fields")
    e.add_argument("--no-email", action="store_true", help="skip email alert")

    sub.add_parser("clear", help="clear the killswitch (human ack)")

    args = parser.parse_args()
    {
        "check": _cmd_check,
        "status": _cmd_status,
        "engage": _cmd_engage,
        "clear": _cmd_clear,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
