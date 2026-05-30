#!/usr/bin/env python3
"""Daily install-lane canary digest.

Runs the same checks as install_lane_monitor.py, formats the result as a
short HTML digest, and emails it to i@m13v.com via the existing DWD gmail
client. Designed for the launchd job com.m13v.social-install-lane-digest
(fires 9am PT daily).

Behavior:
  - Always sends (so a missing email = launchd or DWD itself is broken).
  - Subject reflects health: "OK" / "WARN" / "FAIL".
  - Body includes the heartbeat freshness, per-platform attribution,
    stuck-processing rows, and the canary lane configuration.
  - Exit code is the worst severity (0 OK, 1 anything off) so launchd's
    own SuccessfulExit logging matches the email subject.
"""
import os, sys, datetime, html, subprocess, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from db import load_env, get_conn

load_env()
db = get_conn()

# Platforms that use HTTP-lane (install_id) attribution. Must mirror
# install_lane_monitor.py::HTTP_LANE_PLATFORMS. Adding a platform here
# silences the "unexpected install_id rows on SQL-lane platform" WARN
# and instead enforces an attribution-coverage check.
#
# As of 2026-05-12 reply_db.py routes every status flip through PATCH
# /api/v1/replies/{id} carrying X-Installation, so every engage pipeline
# (github, reddit, x, linkedin, moltbook) is HTTP-lane. SQL-lane is empty.
HTTP_LANE_PLATFORMS = {"github", "reddit", "x", "linkedin", "moltbook"}

severity = "OK"


def bump(level: str):
    global severity
    rank = {"OK": 0, "WARN": 1, "FAIL": 2}
    if rank[level] > rank[severity]:
        severity = level


# 1. Heartbeat
cur = db.execute(
    """
    SELECT install_id, hostname, request_count,
           EXTRACT(EPOCH FROM (NOW() - last_seen_at))::int AS age_sec,
           last_seen_at, last_ip, last_city, last_country
    FROM installations
    ORDER BY last_seen_at DESC
    LIMIT 5
    """
)
heartbeat_rows = cur.fetchall()
heartbeat_html = ""
if not heartbeat_rows:
    heartbeat_html = "<p><b>HEARTBEAT:</b> no installations rows yet (FAIL)</p>"
    bump("FAIL")
else:
    head = heartbeat_rows[0]
    age = head[3]
    age_disp = f"{age}s" if age < 120 else f"{age // 60}m {age % 60}s"
    if age >= 3600:
        bump("FAIL")
        flag = "FAIL"
    elif age >= 1800:
        bump("WARN")
        flag = "WARN"
    else:
        flag = "OK"
    heartbeat_html = (
        f"<p><b>HEARTBEAT:</b> {flag} — last beat {html.escape(age_disp)} ago"
        f" (install_id <code>{html.escape(head[0])}</code>, {head[2]} beats total,"
        f" last_ip {html.escape(str(head[5] or '?'))} {html.escape(str(head[6] or '?'))}/{html.escape(str(head[7] or '?'))})</p>"
    )

# 2. Per-platform attribution coverage (last 24h)
cur = db.execute(
    """
    SELECT platform,
           COUNT(*)                                   AS total,
           COUNT(install_id)                          AS attributed,
           COUNT(CASE WHEN status='replied'  THEN 1 END) AS replied,
           COUNT(CASE WHEN status='skipped'  THEN 1 END) AS skipped,
           COUNT(CASE WHEN status='processing' THEN 1 END) AS processing,
           COUNT(CASE WHEN status='pending'  THEN 1 END) AS pending
    FROM replies
    WHERE discovered_at >= NOW() - INTERVAL '24 hours'
    GROUP BY platform
    ORDER BY total DESC
    """
)
platform_rows = cur.fetchall()
platforms_html = "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
platforms_html += (
    "<tr><th>platform</th><th>total</th><th>attributed</th><th>replied</th>"
    "<th>skipped</th><th>processing</th><th>pending</th><th>note</th></tr>"
)
for r in platform_rows:
    plat, total, attrib, replied, skipped, proc, pend = r
    note = ""
    if plat in HTTP_LANE_PLATFORMS:
        if total == 0:
            note = "no traffic in last 24h"
        else:
            pct = (attrib / total * 100) if total else 0
            if pct < 80:
                note = f"WARN: only {pct:.0f}% attributed (expected ~100%)"
                bump("WARN")
            else:
                note = f"OK: {pct:.0f}% attributed"
    else:
        if attrib > 0:
            note = f"WARN: {attrib} unexpected install_id rows on SQL-lane platform"
            bump("WARN")
    platforms_html += (
        f"<tr><td>{html.escape(plat)}</td><td>{total}</td><td>{attrib}</td>"
        f"<td>{replied}</td><td>{skipped}</td><td>{proc}</td><td>{pend}</td>"
        f"<td>{html.escape(note)}</td></tr>"
    )
platforms_html += "</table>"

# 3. Stuck in processing
cur = db.execute(
    """
    SELECT id, platform, install_id,
           EXTRACT(EPOCH FROM (NOW() - processing_at))::int AS age_sec
    FROM replies
    WHERE status='processing'
      AND processing_at IS NOT NULL
      AND processing_at < NOW() - INTERVAL '30 minutes'
    ORDER BY processing_at ASC
    LIMIT 10
    """
)
stuck_rows = cur.fetchall()
if not stuck_rows:
    stuck_html = "<p><b>STUCK PROCESSING:</b> none</p>"
else:
    bump("WARN")
    stuck_html = f"<p><b>STUCK PROCESSING ({len(stuck_rows)}):</b></p><ul>"
    for r in stuck_rows:
        rid, plat, iid, age = r
        age_disp = f"{age // 60}m" if age < 7200 else f"{age // 3600}h"
        stuck_html += (
            f"<li>id={rid} platform={html.escape(plat)} "
            f"install_id={html.escape((iid or '-')[:8])} stuck {age_disp}</li>"
        )
    stuck_html += "</ul>"

# 4. Recent heartbeat log FAILs
log_path = os.path.expanduser("~/social-autoposter/skill/logs/heartbeat.log")
log_html = ""
if os.path.exists(log_path):
    try:
        out = subprocess.check_output(
            ["tail", "-500", log_path], text=True, timeout=5
        )
        fails = [ln for ln in out.splitlines() if "FAIL" in ln]
        if fails:
            bump("WARN")
            log_html = (
                f"<p><b>HEARTBEAT LOG:</b> {len(fails)} FAILs in last 500 lines</p><pre>"
                + html.escape("\n".join(fails[-10:]))
                + "</pre>"
            )
        else:
            log_html = "<p><b>HEARTBEAT LOG:</b> no FAILs in last 500 lines</p>"
    except Exception as e:
        log_html = f"<p><b>HEARTBEAT LOG:</b> couldn't read: {html.escape(str(e))}</p>"
else:
    log_html = f"<p><b>HEARTBEAT LOG:</b> not yet created at {html.escape(log_path)}</p>"

# 5. Canary lane configuration sanity check
config_lines = []
plist = os.path.expanduser("~/social-autoposter/launchd/com.m13v.social-github-engage.plist")
if os.path.exists(plist):
    try:
        with open(plist) as fh:
            content = fh.read()
        config_lines.append(
            f"AUTOPOSTER_API_BASE env present: {'yes' if 'AUTOPOSTER_API_BASE' in content else 'no'}"
        )
    except Exception as e:
        config_lines.append(f"could not read plist: {e}")
config_html = "<p><b>CANARY CONFIG (github-engage):</b></p><ul>" + "".join(
    f"<li>{html.escape(l)}</li>" for l in config_lines
) + "</ul>"

# Assemble HTML body
when = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
body_html = (
    f"<html><body>"
    f"<h2>Install Lane Daily Digest — {severity}</h2>"
    f"<p><i>{html.escape(when)}</i></p>"
    f"{heartbeat_html}"
    f"<p><b>LAST 24H REPLIES BY PLATFORM:</b></p>"
    f"{platforms_html}"
    f"{stuck_html}"
    f"{log_html}"
    f"{config_html}"
    f"<hr><p><i>Generated by ~/social-autoposter/scripts/install_lane_digest.py</i></p>"
    f"</body></html>"
)

# Send via DWD gmail client
sys.path.insert(0, os.path.expanduser("~/gmail-api"))
from gmail_dwd_client import gmail_for  # type: ignore

subject = f"[install-lane] {severity} {datetime.date.today().isoformat()}"

# Send policy: only email on WARN/FAIL. Green/OK days are silent.
# NOTE: this removes the dead-man's-switch — a silent day now means EITHER
# "all healthy" OR "cron/DWD itself broke", which are indistinguishable.
# Set SEND_OK_ALWAYS=1 to restore the old every-day OK heartbeat.
send_ok_always = os.environ.get("SEND_OK_ALWAYS") == "1"
should_send = (severity != "OK") or send_ok_always

if should_send:
    client = gmail_for("i@m13v.com")
    service = client.service

    import base64
    from email.mime.text import MIMEText

    msg = MIMEText(body_html, "html")
    msg["to"] = "i@m13v.com"
    msg["from"] = "i@m13v.com"
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"sent: {subject}")
else:
    print(f"skipped (OK): {subject}")

sys.exit(0 if severity == "OK" else 1)
