#!/usr/bin/env python3
"""Generic Gmail-API send helper for Claude-authored reports.

Extracted out of sentry_digest.py so any skill doc (Claude spawned in a
--dangerously-skip-permissions session) can send a report email without
duplicating the Gmail plumbing. Body is read from a file, not a CLI arg, so
large HTML/markdown reports don't hit shell quoting/length limits.

Same Gmail token as strike_alert.py / daily_stats_email.py: i@m13v.com via
~/gmail-api/token_i_at_m13v.com.json.

Usage:
    python3 scripts/send_gmail_report.py \\
        --to i@m13v.com \\
        --subject "[Sentry] s4l: 1 new critical issue" \\
        --body-file /tmp/report.html \\
        [--html]   # body-file content is HTML (default: plain text)
"""

import argparse
import base64
import os
import sys
from email.mime.text import MIMEText

GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]


def _scrub_dashes(s):
    if not s:
        return s
    return s.replace("—", ",").replace("–", ",")


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body-file", required=True, help="Path to a file containing the email body.")
    parser.add_argument("--html", action="store_true", help="Body file content is HTML (default: plain text).")
    args = parser.parse_args()

    with open(args.body_file) as f:
        body = f.read()

    subtype = "html" if args.html else "plain"
    msg = MIMEText(body, subtype)
    msg["to"] = args.to
    msg["subject"] = _scrub_dashes(args.subject)

    service = gmail_service()
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Email sent: {result['id']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR sending email: {e}", file=sys.stderr)
        sys.exit(1)
