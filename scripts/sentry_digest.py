#!/usr/bin/env python3
"""Sentry issue digest for the s4l Sentry project (org mediar-n5).

Replaces the raw per-issue Sentry alert email. That alert rule
(mediar-n5/s4l rule id 17212931) fired on every first-seen issue and every
high-priority mark regardless of severity, so it also emailed on level=warning
menubar operational pings ("draft_stuck", "missing", "rate_limited", "review
card unattended"). Those still exist in Sentry, still get investigated
on-demand via the "Debugging a customer install" playbook (see CLAUDE.md),
they just no longer push a raw email. This script is scoped to level:error and
level:fatal only ("critical Sentry issues" per user instruction 2026-07-07):
real Python exceptions and pipeline failures, not the synthetic warning pings.

Impact ranking uses distinct install_id count, not Sentry's built-in
userCount, because s4l events are tagged per-install (install_id), not
per-Sentry-user (userCount is 0 across the board for this project).

Idempotency / noise control: a JSON ledger at scripts/state/sentry_digest_ledger.json
tracks last-seen event/install counts per issue. A digest email is only sent
when something is NEW (not in the ledger) or GROWING (event count up 20%+ and
by at least 5 events since the ledger snapshot). First run baselines every
open critical issue without flagging all of them as new.

Usage:
    python3 scripts/sentry_digest.py                # normal run (used by launchd)
    python3 scripts/sentry_digest.py --dry-run       # print what would happen, no email, no ledger write

Patterned after strike_alert.py: same Gmail token, same dash-scrubbing,
default recipient i@m13v.com.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText

SENTRY_ORG = "mediar-n5"
SENTRY_PROJECT = "s4l"
SENTRY_PROJECT_ID = "4511598804336640"
SENTRY_API = "https://sentry.io/api/0"

# "Critical Sentry issues" = level:error or level:fatal. Excludes level:warning
# (the synthetic menubar/autopilot signal pings), which are a different kind
# of event and already have their own surfacing (menubar UI, dashboard,
# on-demand Sentry queries during customer debugging).
ISSUE_QUERY = "is:unresolved level:[error,fatal]"

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(REPO_DIR, "scripts", "state")
LEDGER_PATH = os.path.join(STATE_DIR, "sentry_digest_ledger.json")

GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]
NOTIFICATION_EMAIL = os.environ.get("NOTIFICATION_EMAIL", "i@m13v.com")

# Growth threshold: an issue re-alerts if event count grew by at least this
# many events AND by at least this relative fraction since the ledger snapshot.
GROWTH_MIN_DELTA = 5
GROWTH_MIN_RATIO = 1.2


def _scrub_dashes(s):
    if not s:
        return s
    return s.replace("—", ",").replace("–", ",")


def _sentry_token():
    env_token = os.environ.get("SENTRY_AUTH_TOKEN")
    if env_token:
        return env_token
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "sentry-auth-token", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _sentry_get(path, token):
    req = urllib.request.Request(f"{SENTRY_API}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_critical_issues(token):
    path = (
        f"/projects/{SENTRY_ORG}/{SENTRY_PROJECT}/issues/"
        f"?query={urllib.parse.quote(ISSUE_QUERY)}&statsPeriod=24h&sort=freq&limit=100"
    )
    return _sentry_get(path, token)


def fetch_install_impact(issue_id, token):
    """Distinct install_id count + top installs for one issue. Returns (count, top)."""
    try:
        data = _sentry_get(f"/issues/{issue_id}/tags/install_id/", token)
        return data.get("uniqueValues", 0), data.get("topValues", [])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 0, []  # tag not present on any event for this issue
        raise
    except Exception:
        return 0, []


def load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return {"version": 1, "lastUpdated": None, "issues": {}}
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "lastUpdated": None, "issues": {}}


def save_ledger(ledger):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
    os.replace(tmp, LEDGER_PATH)


def issue_link(short_id):
    return f"https://mediar-n5.sentry.io/issues/?project={SENTRY_PROJECT_ID}&query={short_id}"


def gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def send_email(service, to_addr, subject, html_body):
    msg = MIMEText(html_body, "html")
    msg["to"] = to_addr
    msg["subject"] = _scrub_dashes(subject)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return result["id"]


def build_rows_html(rows, growth=False):
    cells = []
    for r in rows:
        link = issue_link(r["shortId"])
        if growth:
            cells.append(
                f"<tr><td><a href='{link}'>{r['shortId']}</a>: {r['title']}</td>"
                f"<td>{r['prevCount']} &rarr; {r['count']}</td>"
                f"<td>{r['prevInstalls']} &rarr; {r['installs']}</td></tr>"
            )
        else:
            cells.append(
                f"<tr><td><a href='{link}'>{r['shortId']}</a>: {r['title']}</td>"
                f"<td>{r['count']}</td><td>{r['installs']}</td></tr>"
            )
    return "".join(cells)


def build_html(new_rows, growing_rows, first_run, total_open):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections = [f"<p><b>Critical Sentry issues (level:error/fatal), s4l project.</b> "
                f"Open total: {total_open}. Generated {today}.</p>"]

    if first_run:
        sections.append("<p>First run: baselining every open critical issue. No investigation, "
                         "just a snapshot. Future runs only flag NEW or GROWING issues.</p>")

    if new_rows:
        sections.append("<h3>New issues</h3><table border='1' cellpadding='6' "
                         "style='border-collapse:collapse'><tr><th>Issue</th><th>Events</th>"
                         "<th>Installs</th></tr>" + build_rows_html(new_rows) + "</table>")

    if growing_rows:
        sections.append("<h3>Growing issues</h3><table border='1' cellpadding='6' "
                         "style='border-collapse:collapse'><tr><th>Issue</th>"
                         "<th>Events (was &rarr; now)</th><th>Installs (was &rarr; now)</th></tr>"
                         + build_rows_html(growing_rows, growth=True) + "</table>")

    if first_run:
        top = sorted(new_rows, key=lambda r: -r["installs"])[:10]
        sections.append("<h3>Top 10 by installs affected (baseline snapshot)</h3>"
                         "<table border='1' cellpadding='6' style='border-collapse:collapse'>"
                         "<tr><th>Issue</th><th>Events</th><th>Installs</th></tr>"
                         + build_rows_html(top) + "</table>")

    sections.append("<p style='color:#888;font-size:12px'>Ranked by distinct install_id count, "
                     "not Sentry's userCount (unset for this project). level:warning menubar "
                     "signals are excluded; they're a different kind of event and still queryable "
                     "in Sentry directly during customer debugging.</p>")
    return "<div style='font-family:sans-serif;max-width:800px'>" + "".join(sections) + "</div>"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print plan, do not send email or write ledger.")
    args = parser.parse_args()

    token = _sentry_token()
    if not token:
        print("ERROR: no Sentry auth token (checked SENTRY_AUTH_TOKEN env and keychain sentry-auth-token)", file=sys.stderr)
        sys.exit(1)

    issues = fetch_critical_issues(token)
    if not isinstance(issues, list):
        print(f"ERROR: unexpected Sentry response: {issues}", file=sys.stderr)
        sys.exit(1)

    ledger = load_ledger()
    known = ledger.get("issues", {})
    first_run = len(known) == 0
    now_iso = datetime.now(timezone.utc).isoformat()

    new_rows, growing_rows = [], []
    updated_issues = dict(known)

    for issue in issues:
        short_id = issue["shortId"]
        count = int(issue.get("count", 0))
        installs, _top = fetch_install_impact(issue["id"], token)
        prev = known.get(short_id)

        if prev is None:
            new_rows.append({"shortId": short_id, "title": issue["title"][:90], "count": count, "installs": installs})
        else:
            prev_count = int(prev.get("lastEventCount", 0))
            if not first_run and count - prev_count >= GROWTH_MIN_DELTA and prev_count > 0 and count >= prev_count * GROWTH_MIN_RATIO:
                growing_rows.append({
                    "shortId": short_id, "title": issue["title"][:90],
                    "count": count, "prevCount": prev_count,
                    "installs": installs, "prevInstalls": prev.get("lastInstallCount", 0),
                })

        updated_issues[short_id] = {
            "title": issue["title"][:200],
            "lastEventCount": count,
            "lastInstallCount": installs,
            "firstSeenRun": (prev or {}).get("firstSeenRun", now_iso),
            "lastSeenRun": now_iso,
        }

    print(f"firstRun={first_run} newCount={len(new_rows)} growingCount={len(growing_rows)} totalOpen={len(issues)}")

    should_email = first_run or new_rows or growing_rows
    if not should_email:
        print("Nothing new or growing. No email.")
        if not args.dry_run:
            ledger["issues"] = updated_issues
            ledger["lastUpdated"] = now_iso
            save_ledger(ledger)
        return

    if first_run:
        subject = f"[Sentry] s4l critical-issue digest live: {len(issues)} baselined"
    elif new_rows and growing_rows:
        subject = f"[Sentry] s4l: {len(new_rows)} new, {len(growing_rows)} growing critical issue(s)"
    elif new_rows:
        top_new = max(new_rows, key=lambda r: r["installs"])
        subject = f"[Sentry] s4l: new critical issue, {top_new['shortId']} ({top_new['installs']} installs)"
    else:
        top_grow = max(growing_rows, key=lambda r: r["installs"])
        subject = f"[Sentry] s4l: {top_grow['shortId']} growing ({top_grow['prevCount']} -> {top_grow['count']} events)"

    html = build_html(new_rows, growing_rows, first_run, len(issues))

    print(f"Subject: {subject}")
    if args.dry_run:
        print("--dry-run: not sending email, not writing ledger.")
        print(html)
        return

    service = gmail_service()
    msg_id = send_email(service, NOTIFICATION_EMAIL, subject, html)
    print(f"Email sent: {msg_id}")

    ledger["issues"] = updated_issues
    ledger["lastUpdated"] = now_iso
    save_ledger(ledger)
    print("Ledger written.")


if __name__ == "__main__":
    main()
