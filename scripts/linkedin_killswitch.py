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
from datetime import datetime, timedelta, timezone
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

# Stop-completely policy (2026-06-03): after the 24h wait, the recovery job runs
# a read-only probe to see if the session healed on its own. We NEVER attempt a
# programmatic login (anti-bot rule). If the probe still shows logged-out after
# this many failed attempts, the session is genuinely dead: we mark the
# killswitch terminal so the hourly job stops probing entirely and a human must
# re-auth + clear. Default 1 == "wait 24h, try once, then stop completely".
RECOVERY_MAX_ATTEMPTS = int(os.environ.get("LINKEDIN_RECOVERY_MAX_ATTEMPTS", "1"))

# Claude-driven re-login (2026-06-03): the read-only probe above only detects a
# self-healed session. The active recovery path instead has the hourly job spin
# up a Claude session that drives the real harness Chrome (the allowed pattern;
# scripted Python login is the banned one) to actually log back in. That session
# returns one of three verdicts, recorded via `recover-record`:
#   - held       -> login succeeded; enter a "pending hold" window and re-verify
#                   (read-only) after RECOVERY_HOLD_CHECK_MINUTES that it STUCK.
#                   Only after a clean hold-check do we clear the flag + resume.
#   - hard_block -> checkpoint / captcha / restriction / wrong creds / 2FA wall.
#                   Terminal immediately: do NOT poke a restricted account again.
#   - transient  -> page didn't load / ambiguous. Re-anchor the 24h clock and let
#                   the next eligible cycle try again, up to RECOVERY_TRANSIENT_MAX.
# If a login held but the session drops during the hold window ("logged out
# shortly after"), the hold-check goes terminal too: try once, don't keep trying.
RECOVERY_HOLD_CHECK_MINUTES = float(
    os.environ.get("LINKEDIN_RECOVERY_HOLD_CHECK_MINUTES", "45")
)
RECOVERY_TRANSIENT_MAX_ATTEMPTS = int(
    os.environ.get("LINKEDIN_RECOVERY_TRANSIENT_MAX_ATTEMPTS", "3")
)

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


_LOGIN_MARKERS = ("/login", "/checkpoint", "/uas/login", "linkedin.com/authwall")


def _probe_linkedin_health(cdp_url, feed_only=False):
    """Gentle, read-only health probe of the LinkedIn session.

    Attaches (CDP) to the already-running linkedin-harness Chrome and does the
    minimal nav set the anti-bot carve-out allows: ONE nav to /feed/ (confirms
    we are logged in) and, unless feed_only, ONE nav to the exact
    /in/me/recent-activity/comments/ endpoint that trips the killswitch (confirms
    it no longer bounces to the authwall). No Voyager calls, no scroll loops, no
    permalink fan-out, no clicks/typing, no programmatic login. Reuses an
    existing tab and never closes the shared context.

    feed_only=True is the per-run detection gate: a single /feed/ nav is enough
    to tell "are we still logged in?" without touching the activity endpoint on
    every healthy pipeline fire.

    Returns (healthy: bool, detail: str, conclusive: bool). Never raises.
    conclusive=True means we definitively observed login state (healthy feed, or
    a redirect to the authwall/login/checkpoint). conclusive=False means we
    could not determine it (CDP attach failed, nav timeout, Chrome down): an
    infra hiccup, NOT evidence the session is dead, so callers must not engage
    the killswitch or count it as a failed re-login attempt on this.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return False, "playwright import failed: {}".format(e), False

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url, timeout=8000)
            except Exception as e:
                return False, "cdp attach failed ({}): {}".format(cdp_url, e), False
            contexts = browser.contexts
            if not contexts:
                return False, "cdp attach: zero contexts", False
            ctx = contexts[0]

            page = None
            reused = False
            for pg in ctx.pages:
                u = pg.url or ""
                if "linkedin.com" in u and "login" not in u and "checkpoint" not in u:
                    page, reused = pg, True
                    break
            if page is None and ctx.pages:
                page, reused = ctx.pages[0], True
            if page is None:
                page = ctx.new_page()

            try:
                # Nav 1: /feed/ — are we still logged in?
                page.goto(
                    "https://www.linkedin.com/feed/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                page.wait_for_timeout(2000)
                u1 = page.url or ""
                if any(m in u1 for m in _LOGIN_MARKERS):
                    return False, "feed redirected to auth: {}".format(u1), True

                if feed_only:
                    title = ""
                    try:
                        title = page.title() or ""
                    except Exception:
                        pass
                    return True, "feed renders (title={!r}, url={})".format(title, u1), True

                # Nav 2: the exact endpoint that engaged the killswitch.
                page.goto(
                    "https://www.linkedin.com/in/me/recent-activity/comments/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                page.wait_for_timeout(2000)
                u2 = page.url or ""
                if any(m in u2 for m in _LOGIN_MARKERS):
                    return False, "activity endpoint redirected to auth: {}".format(u2), True

                title = ""
                try:
                    title = page.title() or ""
                except Exception:
                    pass
                return True, "feed+activity render (title={!r}, url={})".format(title, u2), True
            finally:
                if page is not None and not reused:
                    try:
                        page.close()
                    except Exception:
                        pass
    except Exception as e:
        return False, "probe exception: {}: {}".format(type(e).__name__, e), False


def _send_recovery_email(detail, age_sec):
    """Notify that the killswitch auto-cleared after a healthy probe."""
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
        age_h = round(age_sec / 3600.0, 1) if age_sec else "?"
        subject = "[LI KILL] RECOVERED auto-probe healthy"
        body_lines = [
            "LinkedIn killswitch auto-cleared.",
            "",
            "The hourly recovery probe found the session healthy after the",
            "killswitch had been active for " + str(age_h) + "h, so it cleared",
            "the flag. Every LinkedIn pipeline resumes on its next launchd fire.",
            "",
            "Probe detail: " + str(detail),
            "",
            "If LinkedIn was NOT actually healthy, re-engage manually:",
            "  python3 ~/social-autoposter/scripts/linkedin_killswitch.py \\",
            "    engage --signal manual --detail 'auto-recovery false positive'",
            "",
            "State file: " + STATE_FILE,
            "Trail file: " + TRAIL_FILE,
        ]
        body = _scrub_dashes("\n".join(body_lines))
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = NOTIFICATION_EMAIL
        msg["subject"] = _scrub_dashes(subject)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True, "sent"
    except Exception as exc:
        return False, "send failed: " + str(exc)


def is_terminal():
    """True if auto-recovery has given up (failed re-login after the 24h wait)
    and a human must re-auth + clear. Once terminal, the hourly recovery job
    stops probing entirely."""
    p = read()
    return bool(p and p.get("recovery_terminal"))


def _record_failed_recovery(detail):
    """A read-only recovery probe conclusively showed still-logged-out after the
    24h wait. Increment the attempt counter on the live state file (preserving
    the original ts so age keeps accruing) and, once attempts reach
    RECOVERY_MAX_ATTEMPTS, flip recovery_terminal so we stop completely.

    Returns (attempts: int, terminal: bool)."""
    p = read() or {}
    attempts = int(p.get("recovery_attempts", 0)) + 1
    p["recovery_attempts"] = attempts
    p["last_recovery_ts"] = _now_iso()
    p["last_recovery_detail"] = str(detail)[:2000]
    terminal = attempts >= RECOVERY_MAX_ATTEMPTS
    if terminal:
        p["recovery_terminal"] = True
        p["recovery_terminal_ts"] = _now_iso()
    _ensure_dir()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, indent=2)
        f.write("\n")
    os.replace(tmp, STATE_FILE)
    _append_trail({
        "event": "recovery_failed",
        "ts": _now_iso(),
        "attempts": attempts,
        "terminal": terminal,
        "detail": str(detail)[:500],
    })
    return attempts, terminal


def _send_terminal_email(detail, attempts, age_sec):
    """Notify that auto-recovery gave up; manual re-auth required."""
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
        age_h = round(age_sec / 3600.0, 1) if age_sec else "?"
        subject = "[LI KILL] AUTO-RECOVERY FAILED, manual re-auth required"
        body_lines = [
            "LinkedIn auto-recovery has STOPPED COMPLETELY.",
            "",
            "After the " + str(RECOVERY_MIN_AGE_HOURS) + "h wait, the read-only probe",
            "ran " + str(attempts) + " attempt(s) and the session was still logged out",
            "(redirected to the authwall/login). Per the anti-bot rule we never",
            "log in programmatically, so the hourly recovery job will now stop",
            "probing and every LinkedIn pipeline stays paused until you act.",
            "",
            "Killswitch age at give-up: " + str(age_h) + "h",
            "Last probe detail: " + str(detail),
            "",
            "To resume:",
            "  1. Open the linkedin-harness Chrome (port 9556) and sign back in.",
            "  2. Confirm /feed/ renders without an authwall.",
            "  3. Clear the killswitch:",
            "       python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear",
            "",
            "State file: " + STATE_FILE,
            "Trail file: " + TRAIL_FILE,
        ]
        body = _scrub_dashes("\n".join(body_lines))
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = NOTIFICATION_EMAIL
        msg["subject"] = _scrub_dashes(subject)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True, "sent"
    except Exception as exc:
        return False, "send failed: " + str(exc)


def _write_state(p):
    """Atomically persist the killswitch state dict."""
    _ensure_dir()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, indent=2)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


def _record_login_held():
    """Claude reported a successful re-login. Don't clear the flag yet: enter a
    pending-hold window and re-verify (read-only) after RECOVERY_HOLD_CHECK_MINUTES
    that the session actually stuck. Returns the hold_check_due ISO ts."""
    p = read() or {}
    due = datetime.now(timezone.utc) + timedelta(minutes=RECOVERY_HOLD_CHECK_MINUTES)
    due_iso = due.strftime("%Y-%m-%dT%H:%M:%SZ")
    p["recovery_pending_hold"] = True
    p["hold_check_due"] = due_iso
    p["login_held_ts"] = _now_iso()
    p["recovery_transient_attempts"] = 0
    p.pop("recovery_terminal", None)
    _write_state(p)
    _append_trail({
        "event": "login_held",
        "ts": _now_iso(),
        "hold_check_due": due_iso,
    })
    return due_iso


def _record_hard_block(detail):
    """Claude hit a wall it cannot pass (checkpoint / captcha / restriction /
    wrong creds / 2FA). Go terminal: never auto-poke a restricted account again."""
    p = read() or {}
    p["recovery_terminal"] = True
    p["recovery_terminal_ts"] = _now_iso()
    p["recovery_terminal_reason"] = "hard_block"
    p["last_recovery_detail"] = str(detail)[:2000]
    p.pop("recovery_pending_hold", None)
    p.pop("hold_check_due", None)
    _write_state(p)
    _append_trail({
        "event": "recovery_hard_block",
        "ts": _now_iso(),
        "detail": str(detail)[:500],
    })


def _record_transient(detail):
    """Claude couldn't conclusively log in or fail (page didn't load, ambiguous).
    Re-anchor the 24h clock so the next eligible cycle tries again, up to
    RECOVERY_TRANSIENT_MAX_ATTEMPTS, after which we go terminal.
    Returns (transient_attempts: int, terminal: bool)."""
    p = read() or {}
    attempts = int(p.get("recovery_transient_attempts", 0)) + 1
    p["recovery_transient_attempts"] = attempts
    p["last_recovery_ts"] = _now_iso()
    p["last_recovery_detail"] = str(detail)[:2000]
    terminal = attempts >= RECOVERY_TRANSIENT_MAX_ATTEMPTS
    if terminal:
        p["recovery_terminal"] = True
        p["recovery_terminal_ts"] = _now_iso()
        p["recovery_terminal_reason"] = "transient_exhausted"
    else:
        # Re-anchor age so we wait another full RECOVERY_MIN_AGE_HOURS before the
        # next attempt rather than retrying on the next hourly tick.
        p["ts"] = _now_iso()
    p.pop("recovery_pending_hold", None)
    p.pop("hold_check_due", None)
    _write_state(p)
    _append_trail({
        "event": "recovery_transient",
        "ts": _now_iso(),
        "transient_attempts": attempts,
        "terminal": terminal,
        "detail": str(detail)[:500],
    })
    return attempts, terminal


def _send_simple_email(subject, body_lines):
    """Best-effort plain-text alert to NOTIFICATION_EMAIL. Never raises."""
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
        body = _scrub_dashes("\n".join(body_lines))
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = NOTIFICATION_EMAIL
        msg["subject"] = _scrub_dashes(subject)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True, "sent"
    except Exception as exc:
        return False, "send failed: " + str(exc)


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


def _cmd_detect_gate(args):
    """Per-run logout detector, called by ensure_linkedin_browser_for_backend so
    ANY LinkedIn pipeline trips the killswitch on its natural next fire.

    - If the killswitch is already active: no-op, exit 0 (the file gate / hourly
      recovery already own the situation; don't double-probe).
    - Otherwise run a single read-only /feed/ probe. If it CONCLUSIVELY shows
      logged-out (redirect to authwall/login/checkpoint), engage the killswitch
      (signal login_redirect) and exit 2 so the caller can abort this fire. The
      flag pauses every other pipeline on its next fire and starts the 24h
      recovery clock. Healthy or inconclusive (infra) -> exit 0, proceed."""
    if is_active():
        # Already flagged: nothing to detect. Stay silent + cheap.
        sys.exit(0)
    cdp_url = args.cdp_url or LINKEDIN_CDP_URL
    healthy, detail, conclusive = _probe_linkedin_health(cdp_url, feed_only=True)
    _append_trail({
        "event": "detect_gate",
        "ts": _now_iso(),
        "healthy": healthy,
        "conclusive": conclusive,
        "detail": detail,
    })
    if healthy:
        print("detect-gate: session healthy ({})".format(detail), file=sys.stderr)
        sys.exit(0)
    if not conclusive:
        # Couldn't determine (CDP down, nav timeout). Don't engage on infra
        # noise; let the pipeline's own SESSION_INVALID handling deal with it.
        print("detect-gate: inconclusive ({}), proceeding".format(detail), file=sys.stderr)
        sys.exit(0)
    # Conclusively logged out. Trip the killswitch for the whole fleet.
    run_log_path = os.environ.get("SAPS_RUN_LOG_PATH", "")
    engage(
        signal="login_redirect",
        detail="detect-gate: {}".format(detail),
        run_log_path=run_log_path,
        extra={"detected_by": os.environ.get("SAPS_PIPELINE_NAME", "?"), "probe": "feed_only"},
        send_email=not args.no_email,
    )
    print(
        "detect-gate: LOGGED OUT, killswitch ENGAGED ({}); aborting this fire".format(detail),
        file=sys.stderr,
    )
    sys.exit(2)


def _hold_check_due_seconds():
    """Seconds until (negative) / since (positive) the pending-hold re-verify is
    due. None if not in a pending-hold window or the ts is unparseable."""
    p = read()
    if not p or not p.get("recovery_pending_hold"):
        return None
    due = _parse_ts(p.get("hold_check_due", ""))
    if due is None:
        return None
    return (datetime.now(timezone.utc) - due).total_seconds()


def _cmd_recover_check(args):
    """Gate for the hourly recovery job. Exits 0 when there is work to do and
    prints the MODE on stdout so the shell knows which path to drive:

      "login" -> killswitch active >= RECOVERY_MIN_AGE_HOURS and not mid-hold:
                 spin up the Claude re-login session, then `recover-record`.
      "hold"  -> a prior login succeeded and the hold window has elapsed: run the
                 read-only `recover-hold` re-verify (no Claude, no login).

    Exits 1 (no stdout) when there is nothing to do this hour (inactive,
    terminal, too young, or still inside an unelapsed hold window)."""
    if not is_active():
        print("recover-check: killswitch not active, nothing to recover", file=sys.stderr)
        sys.exit(1)
    if is_terminal():
        print(
            "recover-check: TERMINAL (auto-recovery gave up); "
            "manual re-auth + clear required, not probing",
            file=sys.stderr,
        )
        sys.exit(1)

    # Pending-hold takes priority: a login already succeeded; we are only waiting
    # to confirm it stuck. No new login attempt while in this window.
    hold_age = _hold_check_due_seconds()
    if hold_age is not None:
        if hold_age >= 0:
            print(
                "recover-check: hold-check due ({:.0f}m past due), re-verifying".format(
                    hold_age / 60.0
                ),
                file=sys.stderr,
            )
            print("hold")
            sys.exit(0)
        print(
            "recover-check: login holding, hold-check in {:.0f}m".format(
                -hold_age / 60.0
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    age = age_seconds()
    min_age = RECOVERY_MIN_AGE_HOURS * 3600
    if age is None:
        print(
            "recover-check: active but ts unparseable, manual clear required",
            file=sys.stderr,
        )
        sys.exit(1)
    if age < min_age:
        print(
            "recover-check: active but only {:.1f}h old (< {}h), waiting".format(
                age / 3600.0, RECOVERY_MIN_AGE_HOURS
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        "recover-check: eligible for re-login (active {:.1f}h >= {}h)".format(
            age / 3600.0, RECOVERY_MIN_AGE_HOURS
        ),
        file=sys.stderr,
    )
    print("login")
    sys.exit(0)


def _cmd_recover(args):
    """Run the gentle probe (Chrome must already be up); clear + email on health.

    Re-checks the age gate itself (unless --force) so it is safe to call
    directly, not just behind recover-check."""
    if not is_active():
        print(json.dumps({"recovered": False, "reason": "not_active"}))
        sys.exit(0)
    if is_terminal():
        print(json.dumps({"recovered": False, "reason": "terminal_manual_required"}))
        sys.exit(0)
    age = age_seconds()
    min_age = RECOVERY_MIN_AGE_HOURS * 3600
    if not args.force and (age is None or age < min_age):
        print(json.dumps({
            "recovered": False,
            "reason": "too_young",
            "age_hours": (round(age / 3600.0, 2) if age else None),
        }))
        sys.exit(0)

    cdp_url = args.cdp_url or LINKEDIN_CDP_URL
    healthy, detail, conclusive = _probe_linkedin_health(cdp_url)
    _append_trail({
        "event": "recover_probe",
        "ts": _now_iso(),
        "healthy": healthy,
        "conclusive": conclusive,
        "detail": detail,
        "age_hours": (round(age / 3600.0, 2) if age else None),
    })
    if not healthy:
        # Inconclusive (CDP down, nav timeout): infra hiccup, not a dead
        # session. Do NOT count it as a failed re-login; just retry next hour.
        if not conclusive:
            print(json.dumps({
                "recovered": False,
                "reason": "probe_inconclusive",
                "detail": detail,
            }))
            sys.exit(0)
        # Conclusively still logged out after the 24h wait. Record the failed
        # attempt; once we hit RECOVERY_MAX_ATTEMPTS we stop completely.
        attempts, terminal = _record_failed_recovery(detail)
        if terminal and not args.no_email:
            ok, msg = _send_terminal_email(detail, attempts, age)
            _append_trail({"event": "terminal_email", "ok": ok, "msg": msg})
        print(json.dumps({
            "recovered": False,
            "reason": ("recovery_terminal" if terminal else "relogin_failed_retrying"),
            "attempts": attempts,
            "terminal": terminal,
            "detail": detail,
        }))
        sys.exit(0)

    clear()
    _append_trail({"event": "recover_clear", "ts": _now_iso(), "detail": detail})
    if not args.no_email:
        ok, msg = _send_recovery_email(detail, age)
        _append_trail({"event": "recover_email", "ok": ok, "msg": msg})
    print(json.dumps({"recovered": True, "detail": detail}))
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

    dg = sub.add_parser(
        "detect-gate",
        help="per-run logout probe; engage + exit 2 if conclusively logged out",
    )
    dg.add_argument("--cdp-url", default="", help="harness CDP URL (default $LINKEDIN_CDP_URL)")
    dg.add_argument("--no-email", action="store_true", help="skip engage alert email")

    sub.add_parser(
        "recover-check",
        help="exit 0 if active AND >= RECOVERY_MIN_AGE_HOURS old (else 1)",
    )

    r = sub.add_parser(
        "recover",
        help="gentle probe; clear + email on health (Chrome must be up)",
    )
    r.add_argument("--cdp-url", default="", help="harness CDP URL (default $LINKEDIN_CDP_URL)")
    r.add_argument("--no-email", action="store_true", help="skip recovery email")
    r.add_argument("--force", action="store_true", help="skip the age gate")

    args = parser.parse_args()
    {
        "check": _cmd_check,
        "status": _cmd_status,
        "engage": _cmd_engage,
        "clear": _cmd_clear,
        "detect-gate": _cmd_detect_gate,
        "recover-check": _cmd_recover_check,
        "recover": _cmd_recover,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
