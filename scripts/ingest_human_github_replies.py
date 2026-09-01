#!/usr/bin/env python3
"""Ingest human replies to GitHub escalation emails from Gmail and post them.

GitHub-lane mirror of ingest_human_dm_replies.py. Flow:
  1. engage_github.py decides action='escalate' -> reply_db.py escalated ->
     POST /api/v1/replies/{id}/flag-human sets status='escalated' and sends an
     escalation email with subject `[GH #<reply_id>] <author> [github]: <reason>`
     FROM matt@s4l.ai TO NOTIFICATION_EMAIL (i@m13v.com).
  2. The human hits Reply in Gmail and writes the answer. Because the
     escalation's From is matt@s4l.ai (a send-as alias), the reply lands in the
     matt@s4l.ai mailbox as a fresh unread inbound message.
  3. This script polls that mailbox for unread `Re: [GH #N]` messages. For each,
     it extracts the reply_id, strips quoted history, and posts the text
     VERBATIM as a comment on the GitHub thread via gh CLI (unlike the DM lane,
     there is no rewrite step; what the human wrote is what appears).
  4. The replies row flips to status='replied' with the human's text and the
     posted comment URL; "skip"/"ignore"/"not relevant" bodies dismiss the
     escalation instead (status='skipped', no comment posted).
  5. The Gmail message is marked read so it is never re-ingested; rows not in
     status='escalated' are never posted (double-post guard for stale emails).

Auth: same keyless DWD lane as the DM ingest (helpers imported from
ingest_human_dm_replies.py, which owns the token refresh logic).

Usage:
    python3 scripts/ingest_human_github_replies.py             # ingest and post
    python3 scripts/ingest_human_github_replies.py --dry-run   # print actions, no posts, no DB writes, no label changes
"""

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_patch
from ingest_human_dm_replies import (
    gmail_service,
    fetch_raw,
    pick_plain_body,
    strip_quoted_history,
    extract_sender_addr,
)

GH_ID_RE = re.compile(r"\[GH\s*#(\d+)\]", re.IGNORECASE)
RE_PREFIX_RE = re.compile(r"^\s*re\s*:", re.IGNORECASE)
GMAIL_QUERY = 'is:unread subject:"Re: [GH #"'
DISMISS_WORDS = {"skip", "ignore", "not relevant"}

ISSUE_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)")


def parse_issue_coords(url):
    m = ISSUE_URL_RE.search(url or "")
    if not m:
        return None, None, None
    return m.group(1), m.group(2), int(m.group(3))


def post_comment(owner, repo, number, body):
    """Post via gh CLI; returns (ok, url_or_error). Same shape as engage_github."""
    try:
        out = subprocess.check_output(
            ["gh", "issue", "comment", str(number), "-R", f"{owner}/{repo}", "--body", body],
            text=True, timeout=60, stderr=subprocess.STDOUT,
        )
        for line in out.strip().splitlines():
            if line.startswith("https://github.com"):
                return True, line.strip()
        return True, None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        err = e.output if hasattr(e, "output") and e.output else str(e)
        return False, str(err)[:300]


def mark_read(service, gmail_id):
    try:
        service.users().messages().modify(
            userId="me", id=gmail_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except Exception as e:
        print(f"  WARN {gmail_id}: could not mark as read: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be posted, do not post/patch/mark-read")
    args = parser.parse_args()

    try:
        service = gmail_service()
    except Exception as e:
        print(f"FATAL: could not build Gmail service: {e}", file=sys.stderr)
        sys.exit(2)

    resp = service.users().messages().list(userId="me", q=GMAIL_QUERY, maxResults=50).execute()
    candidates = resp.get("messages", []) or []
    if not candidates:
        print("No candidate Gmail messages for GitHub escalation replies.")
        return

    posted = 0
    dismissed = 0
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
        sender = extract_sender_addr(email_msg.get("From", ""))
        m = GH_ID_RE.search(subject)
        if not m:
            print(f"  SKIP {gmail_id}: subject has no [GH #N] token ({subject!r})")
            skipped += 1
            continue
        reply_id = int(m.group(1))

        # Reject forwards / originals: only true Gmail replies count.
        if not RE_PREFIX_RE.match(subject):
            print(f"  SKIP {gmail_id}: subject not a reply ({subject!r})")
            skipped += 1
            continue

        r_resp = api_get(f"/api/v1/replies/{reply_id}", ok_on_404=True)
        row = (r_resp.get("data") or {}).get("reply") if r_resp.get("ok") else None
        if not row:
            print(f"  SKIP {gmail_id}: reply #{reply_id} not found")
            skipped += 1
            if not args.dry_run:
                mark_read(service, gmail_id)
            continue

        # Double-post guard: only an 'escalated' row is actionable. A row
        # already flipped to replied/skipped means an earlier run (or a second
        # email) handled it; mark the mail read and move on.
        if row.get("status") != "escalated":
            print(f"  SKIP {gmail_id}: reply #{reply_id} status is "
                  f"'{row.get('status')}', not 'escalated'")
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

        owner, repo, number = parse_issue_coords(row.get("their_comment_url") or "")
        if not owner:
            print(f"  SKIP {gmail_id}: reply #{reply_id} has no parseable "
                  f"their_comment_url ({row.get('their_comment_url')!r})")
            skipped += 1
            continue

        if reply_text.strip().lower().rstrip(".!") in DISMISS_WORDS:
            print(f"  DISMISS {gmail_id}: reply #{reply_id} "
                  f"({owner}/{repo}#{number}) per '{reply_text.strip()}' from {sender}")
            if not args.dry_run:
                api_patch(f"/api/v1/replies/{reply_id}", {
                    "status": "skipped",
                    "skip_reason": "escalation_dismissed_by_human",
                })
                mark_read(service, gmail_id)
            dismissed += 1
            continue

        print(f"  POST {gmail_id}: reply #{reply_id} -> {owner}/{repo}#{number}: "
              f"{reply_text[:120]!r}")
        if args.dry_run:
            posted += 1
            continue

        ok_post, url_or_err = post_comment(owner, repo, number, reply_text)
        if not ok_post:
            # Leave unread + escalated so the next run retries.
            print(f"  ERROR {gmail_id}: gh comment failed: {url_or_err}")
            skipped += 1
            continue

        api_patch(f"/api/v1/replies/{reply_id}", {
            "status": "replied",
            "our_reply_content": reply_text,
            "our_reply_url": url_or_err,
            "engagement_style": "human_escalation",
        })
        mark_read(service, gmail_id)
        posted += 1
        print(f"  DONE reply #{reply_id} -> {url_or_err or '(no url)'}")

    print(f"Done. Posted={posted} dismissed={dismissed} skipped={skipped} "
          f"candidates={len(candidates)}")


if __name__ == "__main__":
    main()
