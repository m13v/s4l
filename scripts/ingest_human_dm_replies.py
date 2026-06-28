#!/usr/bin/env python3
"""Ingest human replies to DM escalation emails from Gmail into human_dm_replies.

Flow:
  1. flag_human() in dm_conversation.py sends an escalation email with subject
     `[DM #<id>] <author> [<platform>]: <reason>` FROM matt@s4l.ai TO
     NOTIFICATION_EMAIL (i@m13v.com).
  2. The human reads it in i@m13v.com, hits Reply in Gmail, writes what they want
     to say, sends. Gmail keeps `[DM #<id>]` in the subject (prefixed with Re:).
     Because the escalation's From is matt@s4l.ai, the reply is delivered TO the
     matt@s4l.ai mailbox as a fresh unread inbound message (the reply is sent from
     i@m13v.com, a different account, so it lands in matt@s4l.ai's inbox, not Sent).
  3. This script polls the matt@s4l.ai mailbox for messages matching that subject
     token that are unread. For each, it extracts the dm_id, strips the quoted
     history from the reply body, and inserts a row into human_dm_replies with
     status='pending' (unique on gmail message id).
  4. It marks the Gmail message as read so we don't re-ingest.
  5. Phase 0 of skill/engage-dm-replies.sh then picks up pending rows and sends
     them as DMs on the target platform.

  Auth: matt@s4l.ai is reached via the keyless Domain-Wide Delegation lane (the
  service account gmail-dwd-impersonator impersonates it; s4l.ai is a secondary
  domain in the mediar.ai Workspace). The short-lived access token is kept warm by
  launchd job com.m13v.gmail-dwd-keepalive-s4l; this script also refreshes inline
  if the token is missing or about to expire.

Usage:
    python3 scripts/ingest_human_dm_replies.py             # ingest and report
    python3 scripts/ingest_human_dm_replies.py --dry-run   # print what would be ingested, no DB writes, no label changes
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from email import message_from_bytes
from email.policy import default as email_default_policy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# HTTP-only: dms lookup + human_dm_replies dedup/insert go through the s4l.ai
# HTTP API (scripts/http_api.py). The direct-Postgres lane was removed
# 2026-06-01; DATABASE_URL is deliberately ignored, no DB path, no fallback.
from http_api import api_get, api_post

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GMAIL_SCOPES = ["https://mail.google.com/"]

# matt@s4l.ai via keyless Domain-Wide Delegation. The keepalive launchd job
# (com.m13v.gmail-dwd-keepalive-s4l) keeps this access token warm; we also
# refresh inline below if it is missing or within 60s of expiry.
DWD_CREDS_PATH = os.path.expanduser("~/.gmail-mcp-s4l/credentials.json")
DWD_REFRESHER = os.path.expanduser("~/gmail-dwd-keepalive/refresh_token_s4l.py")
DWD_PYTHON = os.path.expanduser("~/gmail-dwd-keepalive/.venv/bin/python")

SELF_ADDRESSES = {"matt@s4l.ai"}
DM_ID_RE = re.compile(r"\[DM\s*#(\d+)\]", re.IGNORECASE)
RE_PREFIX_RE = re.compile(r"^\s*re\s*:", re.IGNORECASE)

# Only fetch Gmail replies (subject starts with "Re:"). This excludes both our
# own outgoing escalation emails (original subject, no Re:) and stale historical
# escalations that pre-date the rewire.
GMAIL_QUERY = 'is:unread subject:"Re: [DM #"'


def _run_refresher():
    subprocess.run([DWD_PYTHON, DWD_REFRESHER], check=True, timeout=60)


def gmail_service():
    """Build the Gmail service for matt@s4l.ai from the DWD access token.

    Reads the short-lived token minted by the keepalive job. If the file is
    missing or the token is within 60s of expiry, runs the refresher inline.
    """
    if not os.path.exists(DWD_CREDS_PATH):
        _run_refresher()
    with open(DWD_CREDS_PATH) as f:
        payload = json.load(f)
    expiry_ms = payload.get("expiry_date", 0)
    if not payload.get("access_token") or (expiry_ms and expiry_ms / 1000 <= time.time() + 60):
        _run_refresher()
        with open(DWD_CREDS_PATH) as f:
            payload = json.load(f)
    creds = Credentials(token=payload["access_token"], scopes=GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_candidate_messages(service):
    resp = service.users().messages().list(userId="me", q=GMAIL_QUERY, maxResults=50).execute()
    return resp.get("messages", []) or []


def fetch_raw(service, message_id):
    msg = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    raw = base64.urlsafe_b64decode(msg["raw"].encode("ASCII"))
    return message_from_bytes(raw, policy=email_default_policy), msg.get("labelIds", [])


def pick_plain_body(email_msg):
    if email_msg.is_multipart():
        text_part = None
        for part in email_msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
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


# Common "On Mon, Apr 20, 2026 at 5:30 PM X wrote:" patterns across clients.
QUOTE_MARKER_RES = [
    re.compile(r"^On .{5,200}\s+wrote:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From:\s.+<.+>\s*$", re.MULTILINE),
]


def strip_quoted_history(body):
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


def extract_sender_addr(raw_from):
    if not raw_from:
        return ""
    m = re.search(r"<([^>]+)>", raw_from)
    return (m.group(1) if m else raw_from).strip().lower()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print what would be ingested, do not touch DB or labels")
    args = parser.parse_args()

    try:
        service = gmail_service()
    except Exception as e:
        print(f"FATAL: could not build Gmail service: {e}", file=sys.stderr)
        sys.exit(2)

    candidates = list_candidate_messages(service)
    if not candidates:
        print("No candidate Gmail messages for DM escalation replies.")
        return

    ingested = 0
    skipped = 0
    for c in candidates:
        gmail_id = c["id"]
        try:
            email_msg, labels = fetch_raw(service, gmail_id)
        except Exception as e:
            print(f"  SKIP {gmail_id}: fetch failed: {e}")
            skipped += 1
            continue

        subject = email_msg.get("Subject", "") or ""
        sender = extract_sender_addr(email_msg.get("From", ""))
        m = DM_ID_RE.search(subject)
        if not m:
            print(f"  SKIP {gmail_id}: subject has no [DM #N] token ({subject!r})")
            skipped += 1
            continue
        dm_id = int(m.group(1))

        # Belt-and-suspenders: reject anything where the subject doesn't start
        # with Re:. The Gmail query should already filter this, but if someone
        # forwards an escalation, is:unread + [DM #N] would match without Re:.
        if not RE_PREFIX_RE.match(subject):
            print(f"  SKIP {gmail_id}: subject not a reply ({subject!r})")
            skipped += 1
            continue

        dm_resp = api_get(f"/api/v1/dms/{dm_id}", ok_on_404=True)
        dm_row = (dm_resp.get("data") or {}).get("dm") if dm_resp.get("ok") else None
        if not dm_row:
            print(f"  SKIP {gmail_id}: DM #{dm_id} not found in dms table")
            skipped += 1
            continue

        body_raw = pick_plain_body(email_msg)
        reply_text = strip_quoted_history(body_raw)
        if not reply_text:
            print(f"  SKIP {gmail_id}: empty reply after stripping quoted history")
            skipped += 1
            continue

        project = dm_row.get("target_project") or dm_row.get("project_name")

        print(f"  MATCH {gmail_id}: DM #{dm_id} ({dm_row['platform']}/{dm_row['their_author']}) reply {reply_text!r}")

        if args.dry_run:
            ingested += 1
            continue

        # Dedup-on-gmail-id + insert happen server-side in one POST: the
        # endpoint SELECTs by resend_email_id and returns reused=true if the
        # reply was already ingested, otherwise inserts status='pending'.
        try:
            resp = api_post("/api/v1/human-dm-replies", {
                "dm_id": dm_id,
                "platform": dm_row["platform"],
                "their_author": dm_row["their_author"],
                "project_name": project,
                "instructions": reply_text,
                "email_subject": subject,
                "resend_email_id": gmail_id,
            })
        except SystemExit as e:
            print(f"  ERROR {gmail_id}: insert failed: {e}")
            skipped += 1
            continue
        data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        reused = bool(data.get("reused"))
        if reused:
            print(f"  SKIP {gmail_id}: already ingested as human_dm_replies #{data.get('id')}")
            skipped += 1

        # Mark as read in both cases so the Gmail query excludes it next run.
        try:
            service.users().messages().modify(
                userId="me", id=gmail_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception as e:
            print(f"  WARN {gmail_id}: could not mark as read: {e}")

        if not reused:
            ingested += 1

    print(f"Done. Ingested={ingested} skipped={skipped} candidates={len(candidates)}")


if __name__ == "__main__":
    main()
