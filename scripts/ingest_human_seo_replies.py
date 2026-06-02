#!/usr/bin/env python3
"""Ingest human replies to SEO escalation emails into seo_escalations.

Mirrors scripts/ingest_human_dm_replies.py one-to-one. The DM version writes
to a separate `human_dm_replies` table; here the reply lives directly on the
seo_escalations row that triggered the email, since the workflow is simpler:
one escalation, one reply, one resume.

Flow:
  1. seo/escalate.py open sends an escalation email with subject
     `[SEO #<id>] <product>/<keyword>: <reason>` from i@m13v.com to
     NOTIFICATION_EMAIL.
  2. The human hits Reply in Gmail. Gmail prefixes "Re:" but keeps
     `[SEO #<id>]` in the subject by default.
  3. This script polls i@m13v.com for `is:unread subject:"Re: [SEO #"`.
     For each match it: extracts the escalation id, strips quoted history
     from the body, and PATCHes /api/v1/seo/escalations
     (action=ingest_reply) which sets status='replied', human_reply,
     replied_at=NOW(), gmail_inbound_id WHERE id=N AND status='pending'.
     All state goes through the s4l.ai HTTP API (HTTP-only, no DATABASE_URL).
  4. Marks the Gmail message as read so it is not re-ingested.
  5. seo/resume_escalations.py (run from cron_seo.sh) picks up rows with
     status='replied' and re-invokes generate_page.py --resume-escalation N.

Usage:
    python3 scripts/ingest_human_seo_replies.py             # ingest and report
    python3 scripts/ingest_human_seo_replies.py --dry-run   # parse only, no writes
"""

import argparse
import base64
import os
import re
import sys
from email import message_from_bytes
from email.policy import default as email_default_policy
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SEO_DIR = SCRIPT_DIR.parent / "seo"
sys.path.insert(0, str(SCRIPT_DIR))
from http_api import api_get, api_patch, load_env  # noqa: E402

load_env()

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GMAIL_TOKEN_PATH = os.path.expanduser("~/gmail-api/token_i_at_m13v.com.json")
GMAIL_SCOPES = ["https://mail.google.com/"]

SEO_ID_RE = re.compile(r"\[SEO\s*#(\d+)\]", re.IGNORECASE)
RE_PREFIX_RE = re.compile(r"^\s*re\s*:", re.IGNORECASE)

# Only fetch Gmail replies (subject starts with "Re:"). The original
# escalation email has no Re: prefix, so this also keeps us from ingesting
# our own outgoing escalations as if they were human replies.
GMAIL_QUERY = 'is:unread subject:"Re: [SEO #"'

ESCALATION_LOG = SEO_DIR / "escalations.log"


def _append_log(line):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(ESCALATION_LOG, "a") as f:
            f.write(f"{ts} {line}\n")
    except OSError:
        pass


def gmail_service():
    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_candidate_messages(service):
    resp = service.users().messages().list(
        userId="me", q=GMAIL_QUERY, maxResults=50,
    ).execute()
    return resp.get("messages", []) or []


def fetch_raw(service, message_id):
    msg = service.users().messages().get(
        userId="me", id=message_id, format="raw",
    ).execute()
    raw = base64.urlsafe_b64decode(msg["raw"].encode("ASCII"))
    return message_from_bytes(raw, policy=email_default_policy), msg.get("labelIds", [])


def pick_plain_body(email_msg):
    """Return the best text/plain body, falling back to text/html stripped of
    tags. Same logic as the DM ingest script; many email clients send both."""
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


def mark_read(service, gmail_id):
    try:
        service.users().messages().modify(
            userId="me", id=gmail_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except Exception as e:
        print(f"  WARN {gmail_id}: could not mark as read: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and print, do not touch DB or Gmail labels")
    args = parser.parse_args()

    try:
        service = gmail_service()
    except Exception as e:
        print(f"FATAL: could not build Gmail service: {e}", file=sys.stderr)
        sys.exit(2)

    candidates = list_candidate_messages(service)
    if not candidates:
        print("No candidate Gmail messages for SEO escalation replies.")
        return

    ingested = 0
    skipped = 0
    for c in candidates:
        gmail_id = c["id"]
        try:
            email_msg, _labels = fetch_raw(service, gmail_id)
        except Exception as e:
            print(f"  SKIP {gmail_id}: fetch failed: {e}")
            skipped += 1
            continue

        subject = email_msg.get("Subject", "") or ""
        m = SEO_ID_RE.search(subject)
        if not m:
            print(f"  SKIP {gmail_id}: subject has no [SEO #N] token ({subject!r})")
            skipped += 1
            continue
        escalation_id = int(m.group(1))

        # Belt-and-suspenders: reject anything where the subject doesn't
        # start with Re:. The Gmail query should already filter this, but
        # forwarded escalations could still match is:unread + token.
        if not RE_PREFIX_RE.match(subject):
            print(f"  SKIP {gmail_id}: subject not a reply ({subject!r})")
            skipped += 1
            continue

        show = api_get("/api/v1/seo/escalations",
                       query={"mode": "show", "id": escalation_id}, ok_on_404=True)
        esc_row = None if show.get("_not_found") else (show.get("data") or {}).get("row")
        if not esc_row:
            print(f"  SKIP {gmail_id}: escalation #{escalation_id} not found")
            skipped += 1
            mark_read(service, gmail_id) if not args.dry_run else None
            continue

        eid = esc_row.get("id")
        status = esc_row.get("status")
        product = esc_row.get("product")
        keyword = esc_row.get("keyword")
        existing_inbound = esc_row.get("gmail_inbound_id")

        # Idempotency: if the row already has an inbound id (already ingested),
        # just mark Gmail as read and move on. If status is not pending, we
        # also skip (cancelled / replied-by-different-msg / resumed).
        if existing_inbound:
            print(f"  SKIP {gmail_id}: escalation #{eid} already ingested as {existing_inbound}")
            skipped += 1
            if not args.dry_run:
                mark_read(service, gmail_id)
            continue
        if status != "pending":
            print(f"  SKIP {gmail_id}: escalation #{eid} status={status} (not pending)")
            skipped += 1
            if not args.dry_run:
                mark_read(service, gmail_id)
            continue

        body_raw = pick_plain_body(email_msg)
        reply_text = strip_quoted_history(body_raw)
        if not reply_text:
            print(f"  SKIP {gmail_id}: empty reply after stripping quoted history")
            skipped += 1
            continue

        print(f"  MATCH {gmail_id}: SEO #{eid} ({product}/{keyword}) reply: {reply_text[:200]!r}")

        if args.dry_run:
            ingested += 1
            continue

        try:
            presp = api_patch("/api/v1/seo/escalations", {
                "id": eid,
                "action": "ingest_reply",
                "human_reply": reply_text,
                "gmail_inbound_id": gmail_id,
            })
            updated = int((presp.get("data") or {}).get("updated") or 0)
            if updated == 0:
                print(f"  ERROR {gmail_id}: UPDATE matched 0 rows (race?); leaving message unread")
                skipped += 1
                continue
        except Exception as e:
            print(f"  ERROR {gmail_id}: update failed: {e}")
            skipped += 1
            continue

        mark_read(service, gmail_id)
        _append_log(
            f"replied #{eid} product={product} keyword=\"{keyword}\" "
            f"gmail_in={gmail_id} reply_len={len(reply_text)}"
        )
        ingested += 1

    print(f"Done. Ingested={ingested} skipped={skipped} candidates={len(candidates)}")


if __name__ == "__main__":
    main()
