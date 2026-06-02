#!/usr/bin/env python3
"""Ingest human (Matt) replies to web-chat escalation emails from Gmail into Postgres.

Mirror of scripts/ingest_human_dm_replies.py for the web-chat rail.

Flow:
  1. The Claude session ends a conversation by sending an escalation email with
     subject `[WEB-CHAT #<thread_db_id>] <project>: <visitor_email>` from
     i@m13v.com to the project's notify_email (or i@m13v.com).
  2. Matt reads it, hits Reply in Gmail, types what he actually wants the
     visitor to see, sends. Gmail keeps `[WEB-CHAT #<id>]` in the subject.
  3. This script polls i@m13v.com for unread replies matching that token,
     extracts the thread_db_id, strips quoted history, INSERTs as a
     sender='founder' message AND fires Resend to the visitor's email.
  4. Marks the Gmail message as read so we don't re-ingest.

Usage:
  python3 ingest_web_chat_replies.py             # ingest and report
  python3 ingest_web_chat_replies.py --dry-run   # print, no DB writes
"""

import argparse
import base64
import os
import re
import subprocess
import sys
from email import message_from_bytes
from email.policy import default as email_default_policy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]

WEB_CHAT_ID_RE = re.compile(r"\[WEB-CHAT\s*#(\d+)\]", re.IGNORECASE)
RE_PREFIX_RE = re.compile(r"^\s*re\s*:", re.IGNORECASE)
GMAIL_QUERY = 'is:unread subject:"Re: [WEB-CHAT #"'

QUOTE_MARKER_RES = [
    re.compile(r"^On .{5,200}\s+wrote:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From:\s.+<.+>\s*$", re.MULTILINE),
]

NODE_BIN = os.path.expanduser("~/.nvm/versions/node/v20.19.4/bin/node")
SEND_REPLY = os.path.expanduser("~/social-autoposter/scripts/send_web_chat_reply.py")


def gmail_service():
    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_candidates(service):
    resp = service.users().messages().list(userId="me", q=GMAIL_QUERY, maxResults=50).execute()
    return resp.get("messages", []) or []


def fetch(service, message_id):
    msg = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    raw = base64.urlsafe_b64decode(msg["raw"].encode("ASCII"))
    return message_from_bytes(raw, policy=email_default_policy)


def pick_plain_body(email_msg):
    if email_msg.is_multipart():
        text_part = None
        for part in email_msg.walk():
            if part.get_content_type() == "text/plain":
                text_part = part
                break
        if text_part is None:
            for part in email_msg.walk():
                if part.get_content_type() == "text/html":
                    text_part = part
                    break
        if text_part is None:
            return ""
        try:
            return text_part.get_content()
        except Exception:
            return text_part.get_payload(decode=True).decode("utf-8", errors="replace")
    try:
        return email_msg.get_content()
    except Exception:
        payload = email_msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
        return email_msg.get_payload() or ""


def strip_quoted(body):
    if not body:
        return ""
    earliest = len(body)
    for pat in QUOTE_MARKER_RES:
        m = pat.search(body)
        if m and m.start() < earliest:
            earliest = m.start()
    trimmed = body[:earliest]
    lines = []
    for line in trimmed.splitlines():
        if line.lstrip().startswith(">"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        service = gmail_service()
    except Exception as e:
        print(f"FATAL: could not build Gmail service: {e}", file=sys.stderr)
        sys.exit(2)

    candidates = list_candidates(service)
    if not candidates:
        print("no candidate Gmail replies for [WEB-CHAT #...]")
        return

    ingested = skipped = 0
    for c in candidates:
        gmail_id = c["id"]
        try:
            email_msg = fetch(service, gmail_id)
        except Exception as e:
            print(f"  SKIP {gmail_id}: fetch failed: {e}")
            skipped += 1
            continue

        subject = email_msg.get("Subject", "") or ""
        m = WEB_CHAT_ID_RE.search(subject)
        if not m:
            print(f"  SKIP {gmail_id}: no [WEB-CHAT #N] token ({subject!r})")
            skipped += 1
            continue
        if not RE_PREFIX_RE.match(subject):
            print(f"  SKIP {gmail_id}: not a reply ({subject!r})")
            skipped += 1
            continue

        thread_db_id = int(m.group(1))
        thread_resp = api_get(
            f"/api/v1/web-chat/thread-by-id/{thread_db_id}", ok_on_404=True
        )
        if thread_resp.get("_not_found"):
            print(f"  SKIP {gmail_id}: thread #{thread_db_id} not found")
            skipped += 1
            continue
        thread = thread_resp.get("data") or {}

        body = pick_plain_body(email_msg)
        reply_text = strip_quoted(body)
        if not reply_text:
            print(f"  SKIP {gmail_id}: empty after stripping quotes")
            skipped += 1
            continue

        # Dedup on gmail id (partial unique index in schema, but pre-check anyway).
        dedup = api_get(
            "/api/v1/web-chat/gmail-ingested", query={"gmail_id": gmail_id}
        )
        already_id = (dedup.get("data") or {}).get("ingested_message_id")
        if already_id:
            print(f"  SKIP {gmail_id}: already ingested as msg #{already_id}")
            skipped += 1
            try:
                service.users().messages().modify(
                    userId="me", id=gmail_id, body={"removeLabelIds": ["UNREAD"]}
                ).execute()
            except Exception:
                pass
            continue

        print(f"  MATCH {gmail_id}: WEB-CHAT #{thread_db_id} ({thread['project_name']}/{thread['visitor_email']}) reply {reply_text[:80]!r}")

        if args.dry_run:
            ingested += 1
            continue

        # Insert as sender='founder' AND fire visitor email via send-email.js
        # (use send_web_chat_reply.py so the dedup + email logic stays in one place).
        # Pass --ingested-gmail-id so the reply endpoint stamps it on the
        # inserted row directly, keeping dedup honest on re-runs.
        try:
            subprocess.run(
                [sys.executable, SEND_REPLY,
                 "--thread", thread["thread_id"],
                 "--text", reply_text,
                 "--name", "matt",
                 "--sender", "founder",
                 "--ingested-gmail-id", gmail_id],
                check=True,
                timeout=60,
            )
        except Exception as e:
            print(f"  ERROR {gmail_id}: send_web_chat_reply.py failed: {e}")
            skipped += 1
            continue

        # Mark gmail as read.
        try:
            service.users().messages().modify(
                userId="me", id=gmail_id, body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except Exception as e:
            print(f"  WARN {gmail_id}: could not mark read: {e}")

        ingested += 1

    print(f"done. ingested={ingested} skipped={skipped} candidates={len(candidates)}")


if __name__ == "__main__":
    main()
