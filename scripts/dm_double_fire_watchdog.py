#!/usr/bin/env python3
"""dm_double_fire_watchdog.py - alert when two pipeline sessions DM the same
thread about the same inbound (the "your AI responder is firing twice" bug,
first reported by HireFireTeam on 2026-07-09; 38 confirmed incidents May-Aug
2026 before the 2026-08-05 Phase A/D platform-scoping fix in
skill/engage-dm-replies.sh).

Detection (pure local log analysis, no Claude tokens):
  1. Scan skill/logs/claude-sessions/<date>/*.jsonl for the last N days and
     extract every `*_browser.py send-dm "<thread_url>" "<text>"` tool call.
  2. Candidate incident: >= 2 sends to the same thread from DIFFERENT session
     files, consecutive sends < 20 minutes apart.
  3. Ground-truth check: map the thread to its dms.id (the session transcripts
     themselves print `=== DM #<id> ... Chat URL: <url>` in history/filter
     tool results), then fetch /api/v1/dms/<id>/messages and confirm NO real
     inbound (excluding __click_signal__ rows) landed between the first and
     last send. A new inbound between sends = legit follow-up, not a dupe.
     If the thread cannot be mapped to a dm id, FAIL OPEN: alert anyway,
     flagged "unverified" (mirrors strike_alert.py's noisy-beats-silent rule).

Alerting: one email per incident to i@m13v.com (NOTIFICATION_EMAIL env
override) via the same Gmail token as strike_alert.py. Idempotent via a state
file of alerted incident keys, so each incident emails exactly once ever.

Wired by launchd com.m13v.social-dm-doublefire-watchdog (hourly) through
skill/dm-doublefire-watchdog.sh.

Usage:
  python3 scripts/dm_double_fire_watchdog.py                # last 2 days, email
  python3 scripts/dm_double_fire_watchdog.py --days 35 --dry-run
  python3 scripts/dm_double_fire_watchdog.py --days 120 --mark-only  # seed state
  python3 scripts/dm_double_fire_watchdog.py --test-email  # prove Gmail path
"""

import argparse
import base64
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

REPO_DIR = os.path.expanduser("~/social-autoposter")
SESSIONS_DIR = os.path.join(REPO_DIR, "skill", "logs", "claude-sessions")
STATE_PATH = os.path.expanduser("~/.claude/social-autoposter/dm_double_fire_alerted.json")
GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]
NOTIFICATION_EMAIL = os.environ.get("NOTIFICATION_EMAIL", "i@m13v.com")

PAIR_WINDOW_SECONDS = 20 * 60

# URL is the anchor; the message text is best-effort (it may be a shell
# variable like "$REPLY", a --file arg, or absent from the command line).
SEND_RE = re.compile(
    r'(?:twitter_browser|reddit_browser|linkedin\w*)\.py\s+send-dm\s+"([^"]+)"(?:\s+"(.{0,300}?)(?:"|$))?',
    re.S,
)
# `=== DM #5612 with HireFireTeam [x] ===\nStatus: ...\nChat URL: https://...`
DM_MAP_RE = re.compile(r"=== DM #(\d+) with (\S+) \[\w+\] ===.{0,200}?Chat URL: (\S+)", re.S)
# filter-inbox / pending JSON rows also carry both keys
DM_JSON_RE = re.compile(r'"dm_id":\s*(\d+).{0,400}?"chat_url":\s*"([^"]+)"', re.S)


def _iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _thread_key(url):
    """Normalize a thread URL to its stable id-pair so /requests/ and
    canonical forms of the same X thread collapse together."""
    m = re.search(r"(?:chat|messages)/(?:requests/)?([\d-]+)", url)
    return m.group(1) if m else url


def scan_sessions(days):
    """Return (sends, thread_to_dm) from the last `days` of session logs.

    sends: list of {ts, thread, key, sess, text}
    thread_to_dm: {thread_key: (dm_id, author)}
    """
    sends = []
    thread_to_dm = {}
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    for day_dir in sorted(glob.glob(os.path.join(SESSIONS_DIR, "*"))):
        if os.path.basename(day_dir) < cutoff_date:
            continue
        for path in glob.glob(os.path.join(day_dir, "*.jsonl")):
            try:
                raw = open(path, errors="ignore").read()
            except OSError:
                continue
            for m in DM_MAP_RE.finditer(raw):
                thread_to_dm[_thread_key(m.group(3))] = (int(m.group(1)), m.group(2))
            for m in DM_JSON_RE.finditer(raw):
                thread_to_dm.setdefault(_thread_key(m.group(2)), (int(m.group(1)), None))
            if "send-dm" not in raw:
                continue
            for line in raw.splitlines():
                if "send-dm" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                content = (rec.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if item.get("type") != "tool_use":
                        continue
                    cmd = (item.get("input") or {}).get("command") or ""
                    sm = SEND_RE.search(cmd)
                    if sm:
                        sends.append({
                            "ts": rec.get("timestamp", ""),
                            "thread": sm.group(1),
                            "key": _thread_key(sm.group(1)),
                            "sess": os.path.basename(path),
                            "text": (sm.group(2) or "(text not inline in command)").replace("\\n", " ")[:200],
                        })
    return sends, thread_to_dm


def find_incidents(sends):
    """Group same-thread sends into incidents: chains of sends < 20 min apart
    involving >= 2 distinct sessions."""
    by_thread = {}
    for s in sends:
        by_thread.setdefault(s["key"], []).append(s)
    incidents = []
    for key, lst in by_thread.items():
        lst.sort(key=lambda s: s["ts"])
        chain = [lst[0]]
        for s in lst[1:]:
            try:
                gap = (_iso(s["ts"]) - _iso(chain[-1]["ts"])).total_seconds()
            except ValueError:
                gap = None
            if gap is not None and gap < PAIR_WINDOW_SECONDS:
                chain.append(s)
            else:
                if len({c["sess"] for c in chain}) >= 2:
                    incidents.append({"key": key, "sends": chain})
                chain = [s]
        if len({c["sess"] for c in chain}) >= 2:
            incidents.append({"key": key, "sends": chain})
    return incidents


def verify_incident(incident, thread_to_dm):
    """Check the DB for a real inbound between first and last send.

    Returns (verdict, dm_id, author) with verdict one of:
      'confirmed'  - no inbound in between: true double-fire
      'follow_up'  - inbound arrived in between: legit, do not alert
      'unverified' - could not map thread to a dm id (fail-open: alert)
    """
    dm = thread_to_dm.get(incident["key"])
    if not dm:
        return "unverified", None, None
    dm_id, author = dm
    t_first = incident["sends"][0]["ts"]
    t_last = incident["sends"][-1]["ts"]
    try:
        sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))
        import http_api
        resp = http_api.api_get(f"/api/v1/dms/{dm_id}/messages", {"limit": 1000})
        msgs = (resp.get("data") or {}).get("messages") or []
    except Exception as exc:  # noqa: BLE001 - fail open on API trouble
        print(f"[watchdog] messages fetch failed for dm {dm_id}: {exc}")
        return "unverified", dm_id, author
    lo, hi = _iso(t_first), _iso(t_last)
    for msg in msgs:
        if msg.get("direction") != "inbound":
            continue
        if (msg.get("author") or "").startswith("__click"):
            continue
        if (msg.get("content") or "").startswith("[CLICK_SIGNAL]"):
            continue
        try:
            at = _iso(msg.get("message_at") or "")
        except ValueError:
            continue
        if lo < at < hi:
            return "follow_up", dm_id, author
    return "confirmed", dm_id, author


def _send_email(subject, body):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    service = build("gmail", "v1", credentials=creds)
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = NOTIFICATION_EMAIL
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def _load_state():
    try:
        return json.load(open(STATE_PATH))
    except (OSError, ValueError):
        return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def format_email(incident, verdict, dm_id, author):
    sends = incident["sends"]
    gap = int((_iso(sends[-1]["ts"]) - _iso(sends[0]["ts"])).total_seconds())
    who = f"@{author}" if author else incident["key"]
    lines = [
        f"Two (or more) pipeline sessions replied to the same DM thread {gap}s apart",
        f"with no new inbound in between. The recipient saw {len(sends)} messages.",
        "",
        f"Recipient: {who}" + (f" (dm_id {dm_id})" if dm_id else ""),
        f"Thread: {sends[0]['thread']}",
        f"Verification: {verdict}"
        + (" (thread could not be mapped to a dms row; inspect manually)" if verdict == "unverified" else ""),
        "",
    ]
    for i, s in enumerate(sends, 1):
        lines += [
            f"Send {i}: {s['ts']}",
            f"  session: {s['sess']}",
            f"  text: {s['text']}",
        ]
    lines += [
        "",
        "This should not happen after the 2026-08-05 platform-scoping fix in",
        "skill/engage-dm-replies.sh (Phase A/D). If you are reading this, either",
        "the fix regressed or a new lane is racing. Investigation playbook:",
        "memory investigation_2026_08_05_dm_double_fire_reddit_twitter_lane_race.",
        "",
        f"Watchdog: scripts/dm_double_fire_watchdog.py on {os.uname().nodename}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=2, help="how many days of session logs to scan (default 2)")
    ap.add_argument("--dry-run", action="store_true", help="print findings; no email, no state writes")
    ap.add_argument("--mark-only", action="store_true", help="record incidents in state without emailing (seed history)")
    ap.add_argument("--limit", type=int, default=10, help="max emails per run (default 10)")
    ap.add_argument("--test-email", action="store_true", help="send a single test email and exit")
    args = ap.parse_args()

    if args.test_email:
        resp = _send_email(
            "S4L DM double-fire watchdog: test email, delivery path OK",
            "This is a one-time test of the double-fire watchdog alert rail.\n"
            "A real alert means two pipeline sessions answered the same DM inbound twice.\n"
            "No action needed for this test.",
        )
        print(f"test email sent: id={resp.get('id')}")
        return

    sends, thread_to_dm = scan_sessions(args.days)
    incidents = find_incidents(sends)
    state = _load_state()
    print(f"[watchdog] scanned {len(sends)} sends over {args.days}d, "
          f"{len(incidents)} candidate incident(s), {len(state)} already alerted")

    emailed = 0
    for inc in sorted(incidents, key=lambda i: i["sends"][0]["ts"]):
        key = f"{inc['key']}|{inc['sends'][0]['ts'][:16]}"
        if key in state:
            continue
        verdict, dm_id, author = verify_incident(inc, thread_to_dm)
        stamp = {"verdict": verdict, "dm_id": dm_id, "author": author,
                 "n_sends": len(inc["sends"]), "detected_at": datetime.now(timezone.utc).isoformat()}
        label = f"@{author}" if author else inc["key"]
        if verdict == "follow_up":
            print(f"  SKIP  {key} {label}: inbound between sends, legit follow-up")
            if not args.dry_run:
                state[key] = stamp
                _save_state(state)
            continue
        if args.dry_run:
            print(f"  ALERT (dry-run) {key} {label}: verdict={verdict} sends={len(inc['sends'])}")
            continue
        if args.mark_only:
            print(f"  MARK  {key} {label}: verdict={verdict} (recorded, not emailed)")
            state[key] = {**stamp, "mark_only": True}
            _save_state(state)
            continue
        if emailed >= args.limit:
            print(f"  HOLD  {key} {label}: per-run email limit {args.limit} reached, will send next run")
            continue
        subject = f"S4L DM double-fire: {label} got {len(inc['sends'])} replies"
        try:
            _send_email(subject, format_email(inc, verdict, dm_id, author))
            emailed += 1
            print(f"  EMAIL {key} {label}: sent")
            state[key] = stamp
            _save_state(state)
        except Exception as exc:  # noqa: BLE001 - leave unalerted for next run
            print(f"  FAIL  {key} {label}: email send failed, will retry next run: {exc}")

    print(f"[watchdog] done, {emailed} email(s) sent")


if __name__ == "__main__":
    main()
