#!/usr/bin/env python3
"""
send_dashboard_invite.py
Send the S4L dashboard onboarding email to a previously-provisioned user.
Reads the user's name and scoped projects from dashboard_users, composes the
invite, and sends via Gmail API from i@m13v.com.

Usage:
    python3 send_dashboard_invite.py <email> [<email> ...]
    python3 send_dashboard_invite.py --dry-run client@example.com
"""

import argparse
import base64
import os
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
if ENV_FILE.exists():
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

FROM_NAME = "Matthew Diakonov"
FROM_EMAIL = "i@m13v.com"
CC_EMAIL = "i@m13v.com"
TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
SCOPES = ["https://mail.google.com/"]
DASHBOARD_URL = "https://app.s4l.ai"


def load_user(email):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT email, name, projects FROM dashboard_users WHERE email=%s",
                (email.lower(),),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"No dashboard_users row for {email}")
            return dict(row)
    finally:
        conn.close()


def build_invite(user):
    name = user.get("name") or user["email"]
    first = name.split()[0] if " " in name else name
    projects = user.get("projects") or []
    scope = ", ".join(projects) if projects else "all projects"
    subject = f"Your S4L dashboard access ({scope})"

    text_body = f"""Hi {first},

I set up dashboard access for you covering {scope}. You'll see live posting, replies, DMs, SEO pages, and weekly stats for {'these projects' if len(projects) > 1 else 'the project'}.

Sign in:
1. Go to {DASHBOARD_URL}
2. Enter {user['email']} and click "Email me a sign-in link"
3. Open the link from your inbox; you'll land in your dashboard

A weekly report covering the same scope will land in your inbox every Monday at 9am.

Reply to this email with any questions.

Matthew
"""

    html_body = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#222;max-width:640px;line-height:1.5;">
<p>Hi {first},</p>
<p>I set up dashboard access for you covering <b>{scope}</b>. You'll see live posting, replies, DMs, SEO pages, and weekly stats for {'these projects' if len(projects) > 1 else 'the project'}.</p>
<p><b>Sign in:</b></p>
<ol>
<li>Go to <a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a></li>
<li>Enter <code>{user['email']}</code> and click "Email me a sign-in link"</li>
<li>Open the link from your inbox; you'll land in your dashboard</li>
</ol>
<p>A weekly report covering the same scope will land in your inbox every Monday at 9am.</p>
<p>Reply to this email with any questions.</p>
<p>Matthew</p>
</body></html>"""

    return subject, text_body, html_body


def gmail_service():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def send(service, to_addr, subject, text_body, html_body):
    msg = MIMEMultipart("alternative")
    msg["from"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["to"] = to_addr
    msg["cc"] = CC_EMAIL
    msg["subject"] = subject
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return result["id"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("emails", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    service = None if args.dry_run else gmail_service()
    for email in args.emails:
        user = load_user(email)
        subject, text_body, html_body = build_invite(user)
        if args.dry_run:
            print(f"--- {email} ---\nSubject: {subject}\n\n{text_body}")
            continue
        mid = send(service, email, subject, text_body, html_body)
        print(f"SENT -> {email}  cc={CC_EMAIL}  id={mid}  subj='{subject}'")


if __name__ == "__main__":
    main()
